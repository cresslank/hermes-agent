# Evidence-owner mapping protocol

The optional `PluginContext.supervision` facade accepts both native immutable
`OwnerRequestV1` values and bounded evidence-owner mappings. No provider imports
are required in the host or retrieval owners.

## Admission

`negotiate("supervision.v1")` advertises `owner_capabilities` only when the calling
owner has a bound current runtime in the same profile and an active supervisor
has the observation/action grant, local data class and an explicit egress policy.
An installed but inactive facade does not replace an existing rank provider.
`owner_deadline=True` means the native host owns a bounded synchronous wait;
call on the authenticated execution thread, not a private worker. The provider
bridge schedules HTTP separately. Async owners must retain the same execution
boundary rather than using `to_thread` for the native decision.

Current owner bindings are `hermes-lcm` -> `lcm` (`history_excerpt` local class)
and `web-muxyard` -> `muxyard` (`public_source` local class). Those classes are
not remote-disclosure grants. Every emitted top-level fact, including
`target_id`, must have an explicit classification in the supervisor's configured
host `egress_policy`; private classes additionally require source grants. Missing
or foreign policy never inherits permission from source content or URL shape.

## Mapping

Exactly these keys are accepted:

- `protocol`: `supervision.v1`.
- `owner`: authenticated owner binding above.
- `event`: `retrieval_candidates` for `rank_candidates`,
  `missing_history_slot` for LCM `expand_one_owned_ref`, or
  `oversized_structured_result` for Muxyard `select_windows`.
- `request_id`: nonempty invocation-local string, at most 128 characters.
- `deadline`: finite absolute monotonic timestamp. Host clamps this to the
  original shared round token; a subsequent call never renews that token.
- `facts`: canonical domain facts, at most eight candidates with exact `id`
  and `ref` (or `source_ref` for windows). Facts are bounded and frozen.
- `completeness`: bounded owner metadata. Exact local source enumeration may
  establish `complete`; archive/global coverage remains unknown.

Mappings cannot provide a revision, generation, data policy, grants, or an
alternate target. The host derives the revision from current execution and
binds the target to `owner:request_id`. Feature-specific facts such as
`constraints_match`, `qualifiers_complete`, currency and supersession are not
invented by the codec. Unknown owner facts remain unknown and features abstain.

Selected (not yet applied) responses echo the literal `request_id` and contain an exact head
permutation (`candidate_ids`), a validated mandatory-preserving subset
(`window_ids`), or one `candidate_id` plus `relation="states_missing_decision"`.
No-op, malformed, stale, expired, unauthorized or unavailable decisions return
`None`; owners retain their original evidence. Rank conflict/isolation IDs are
validated against the same offered set, not treated as trust certification.

## Typed seam

`OwnerRequestV1` adds optional immutable `event`, `facts` and `evidence_refs`.
Empty defaults preserve the previous generic action event. Domain facts are
retained rather than replaced by a generic candidate list. Exact evidence refs
bind proposal evidence separately from candidate IDs. `OwnerDecisionV1.metadata`
retains bounded descriptor metadata; it does not grant authority.

## Verification and limits

`tests/agent/test_supervision_owner_protocol.py` covers native admission and
codecs without external checkouts. `test_supervision_evidence_owners.py` requires
explicit `JEV_SUPERVISOR_SOURCE`, `LCM_SUPERVISION_SOURCE`, and
`MUXYARD_SUPERVISION_SOURCE` isolated source checkouts. It uses real LCM SQLite
recall and Muxyard provider consumers, the native facade, actual Jev registry and
strict fake HTTP transport. No live provider, credential or database is required.
F15's ordinary positive starts with native request assembly and an explicit
`lcm_grep(missing_decision=...)` request, not a fixture-injected missing slot.
The paired bridge must negotiate `exact_expansion=supervision.exact-expansion.v1`
and `owner_consumption=supervision.owner-consumption.v1` before requesting the
literal `expand_one_owned_ref` grant. It is not a ranking/relation/advice alias.

## Consumption acknowledgment

Mapping requests produce `accepted/owner_selected`, never an applied effect.
Their response includes a `receipt_id`, exact `candidate_ids`, and
`consumption=supervision.owner-consumption.v1`. After constructing and validating
the actual view, the same bound owner calls `acknowledge_owner` with exactly
`request_id`, `receipt_id`, `candidate_ids`, and `effect_digest`. The digest is
SHA-256 of the actual JSON effect; null explicitly vetoes it. Revision, deadline,
plugin generation, grant, stop and scope are checked again. Only a successful
acknowledgment allows returning the optional view. Receipt reasons distinguish
`owner_postvalidation`, stale/expired rejection and `owner_consumed:<digest>`.
Unacknowledged selections are rejected at lifecycle close. The existing runtime
settlement seam remains the sole receipt sink; no second durable store is added.

Native consumption is not upstream success, source truth or whole-corpus coverage.
LCM downstream fusion/diversity/coverage/computation validators remain authoritative.

LCM rank selection remains pending through diversity, hydration, exact-reference
validation, seen-ref filtering, response caps and serialization. Its effect digest
is SHA-256 of the **exact UTF-8 serialized `lcm_recall` response** returned to the
caller, not the fused candidate list. The selected winner must survive and the
final delivered identity order/subset must differ from the ordinarily shaped
baseline. Otherwise the owner vetoes the selection and returns that baseline.
The common durable receipt records `consumed` only after acknowledgment succeeds;
failed persistence never authorizes the optional view.

## Exact history read boundary

Request-local visibility includes model-visible content and structured tool-call
arguments (including provider function-call items), but not hidden reasoning.
`history_source_visibility(ref, excerpt)` reports exact-ref availability separately
from source-text visibility. An already available handle stays on the ordinary
`lcm_expand` path without a semantic recovery decision. Missing or oversized
visibility remains unknown, not evidence of absence.

`history_read_fence=supervision.history-read.v1` advertises the host-owner
`begin_history_expansion(selection)` context manager. Only the bound LCM execution
owner may enter it, once per selected receipt, around its existing single bounded
local `lcm_expand` call. It accepts no executable callback, search, command, new
scope, or deadline. Entry revalidates the exact selection under the runtime
revision lock and granting registration fence; both remain held through the local
read and serialize instruction admission and registration revocation. Never put
provider HTTP or waiting inside this lease. After leaving it, the LCM owner still
checks bytes, role, session, lineage, offsets, current source row and the original
deadline before acknowledgment. Selection or lease acquisition alone is not
consumption; an expansion cannot acknowledge without a read lease.

`test_supervision_lcm_consumption.py` exercises these boundaries with ordinary
recall/grep, real request assembly, the full provider registry/MockTransport and
independent reads of real SessionDB receipts.

## Configured typed MCP results

`supervision.mcp-results.v1` registers a pure plugin-side typed result projection.
The actual native MCP handler invokes it on the execution owner after transport
return, outside the MCP RPC lock. It never reparses display prose. Source grants
come from the supervisor's host-policy `mcp_sources` list; each row pins `server`,
`tool`, either `url` or exact `command` and `args`, `accounts`, `sources`, `modes`,
`remote_processing` and `allow_live_fetch`. This policy does not connect a server
or authorize a new call. The acquired server instance must still be in the native
profile-visible connection registry at admission and consumption. Tool/account/
source/mode and route mismatches abstain. Existing per-field egress policy and
`project_excerpt` local observation permission are additionally required.

MCP opportunities retain host-only bindings to each admitted recipient's exact
registration/generation, source grant, adapter, profile-visible server instance
and invoked session. Each recipient independently projects under its own grant;
only identical immutable projections share an opportunity. A generic observation
or field-egress grant cannot borrow another registration's MCP source authority.
Unauthorized observers receive neither facts nor egress policy. Admission is
rechecked immediately before observer dispatch, at proposal submission/selection,
and at final acknowledgment; the latter holds the registration revocation and
MCP transport registry fences through receipt settlement. Independently authorized
recipients remain eligible when another recipient unloads. Conflicting projections
abstain rather than mixing source scopes. A typed request merely claiming the
`mcp` owner, without the real transport binding, cannot dispatch an opportunity.
No new budget is allocated per recipient. Selection remains `accepted/owner_selected`
until the existing owner acknowledgment persists the exact consumed-view digest.
The registered pure projection adapters remain trusted local parsers, not remote
inference or a sandbox for untrusted Python code.

The Switchloom plugin projection consumes `search_context` structuredContent,
requires the server's explicit `candidate_budget` omission before triage,
preserves every item, citation, error/completeness field and allowed transport
metadata, and reorders only the typed item list. Full bounded returned item text
can establish local qualifier integrity, never whole-document/archive coverage.
Missing mode, clipped items and unsupported filters abstain. No automatic
`get_object`, live-mode expansion, provider fetch, server mutation or new visible
tool is introduced. Explicit object reads remain the existing MCP owner's action.
HTTP and stdio route bindings are exercised with synthetic transports only.
