# Native literal-source publication (F22 prerequisite only)

`supervision.literal-sources.v1` is an **opt-in local source assertion** contract.
It does not certify truth, source reliability, accepted claims, contradictions,
correction relevance, provider disclosure, or delivery. F22 remains incomplete.
No canonical host DDL or assertion semantics change is included.

## Registration and authority

The LCM plugin's ordinary `register(ctx)` registers its actual context engine,
then installs its `LiteralSourceProviderV1` adapter through:

```python
ctx.supervision.register_literal_source_owner(
    version="supervision.literal-sources.v1", engine=prototype, provider=adapter)
```

Registration requires the real `PluginContext`, `plugin_id == "hermes-lcm"`,
identity with that manager's currently registered context engine, an explicit
provider implementation, and **host profile policy**:

```yaml
supervision:
  enabled: true
  plugins:
    hermes-lcm:
      literal_sources:
        version: supervision.literal-sources.v1
        recipients: [example-supervisor]
    example-supervisor:
      grants: [observe]
      data_policy: [history_excerpt]
```

This is not an activation instruction. The default is no source registration.
The recipient must separately register requesting `observe`. A source grant to
one recipient never authorizes another. Egress requires a separate existing
provider policy and is not performed by this API.

`negotiate("supervision.v1")` advertises `literal_sources` as the version;
`literal_source_owner` is true only for a current registered source owner and
its actual active engine. A callable attribute, copied mapping, schema label,
checksum, or unrelated plugin `facts` dictionary is not a source capability.
The threat boundary is the trusted in-process plugin registration system, not a
sandbox against arbitrary Python code that tampers with private host objects.

LCM's native `clone_for_agent()` binds legitimate clones to the same provider.
The provider pins each clone's store object, home and configured database;
publication additionally requires identity with the active agent's
`context_compressor`. Storage rebinding or another clone/profile loses authority.

## Ordinary caller and exact typed API

`LCMEngine.handle_tool_call()` invokes the existing pack/compile descriptors.
Their `literal_source_tool` wrapper begins one native invocation. The actual
`evidence_pack._resolve_candidate` hydrator observes its already fetched row;
proposal compilation reaches this path through `_validate_claim`. Auto
compilation has a separate hydrator, `requirements_compiler._normalize_ref`,
which is also wired. There is no source scan, extra evidence retrieval, assertion
lookup, or inference call. Publication and consumption perform bounded **point
revalidation of only those original row IDs**.

Host-owned types live in `agent.supervision_literal_sources`:

```python
@dataclass(frozen=True)
class LiteralSourceRecordV1:
    schema_id: str
    exact_ref: str
    row_hash: str
    record_span: tuple[int, int]
    source_bytes: bytes
    coordinate_pins: tuple[tuple[str, str], ...]
    source_attribution: tuple[tuple[str, str], ...]

@dataclass(frozen=True)
class SourcePropositionV1:
    ref: str
    owner_id: str
    owner_generation: str
    invocation_id: str
    revision: Revision
    deadline: float
    record: LiteralSourceRecordV1
```

Coordinates and attribution are tuples of `(field_name, canonical_JSON_value)`;
unknown native attribution is encoded as JSON `null`, not invented. Original
UTF-8 bytes remain separate and unchanged. Exact refs/spans use LCM **Unicode
character offsets**, not byte offsets. The row SHA-256 pins content and native
role/source/session/conversation/tool/observation/ingestion attribution. It is a
source-version integrity pin, not semantic proof or truth.

Facade calls:

- `begin_literal_sources(version=..., engine=...) -> LiteralSourceInvocationV1 | None`
- `publish_literal_sources(version=..., invocation=..., source_refs=tuple[str, ...]) -> tuple[str, ...]`
- `cancel_literal_sources(version=..., invocation=...) -> None`
- `literal_source(version=..., ref=str) -> SourcePropositionV1 | None`

The invocation is an identity-checked native object with ID, exact revision,
original shared deadline, active engine and opaque native lifecycle binding.
Publication consumes it **once** and
resolves opaque source handles through the registered provider; it accepts no
record payload. A copied dataclass cannot publish or cancel it. The unchanged
public tool argument schemas receive no new keys; successful tool output may
include `source_propositions`, a list of opaque host refs. Copying that list
confers no authority.

The same-window native publication consumer can call:

```python
lookup_literal_source(runtime, ref, recipient_registration)
```

Pass the actual active `_Registration`, never a name/generation copied from a
payload. Lookup requires registry **object identity**, the correct profile,
explicit source recipient permission, active `observe` and `history_excerpt`
grants, current source registration/generation, active engine identity, exact
revision and unexpired original deadline. It rereads the source row and compares
all immutable record fields, then rechecks native authority before returning.
A plugin consumer uses its own facade's `literal_source`, which supplies its own
registration. Call lookup outside the graph/registration locks: native source
point reads precede the final fence, whose lock order is runtime then source
registration then recipient then the provider's short lifecycle fence. The
provider exposes `literal_source_binding(engine)` and
`literal_source_binding_fence(engine, binding)`; these must not read source rows
or acquire host graph locks. The same fence invalidates the generation before
session start/end/reset/shutdown mutations. Capture remains unavailable during
lifecycle transitions, and A→B→A never revives a prior generation. Only a new
invocation after a successful start can publish again. Older adapters default
to unavailable, preserving ordinary output rather than publishing unfenced pins.
Registry membership is checked before taking the
recipient fence; registry replacement irreversibly closes the old registration.
Foreign provider/profile, grant loss, unload/replacement, source
mutation, role change, session rebinding, revision change and expiry all abstain.

## Bounds and lifetime

- Whole source record: at most 2400 UTF-8 bytes, plus at most 4096 bytes of native
  attribution metadata. No cap truncation qualifies.
- At most 8 records/publication, 8 pending invocations, 64 retained records per
  source registration. Overflow rejects the entire publication, not a prefix.
- Immutable source and host records are bounded **in-memory**, not a new corpus.
  Expired/stale records are pruned on begin/lookup; unload clears retained state.
- The existing shared tool-round deadline is used unchanged (currently at most
  150 ms). There is no renewal on lookup, clock override, delayed provider worker,
  durable restart promise, or automatic retrieval to restore missing records.
- A returned proposition is a snapshot, not a perpetual capability. Every effect
  using **v1 publication authority** must reenter lookup. No post-return concurrent-mutation
  exclusion is promised. Parent joins must perform their final source check at
  their effect boundary and explicitly design any later durable adoption or
  fresh-invocation renewal; copying an expired source proposition is insufficient.

## Grammar boundary and remaining joins

`lcm.literal-record.v1` is documented in LCM's `docs/literal-record.md`. Only
complete original user/tool rows qualify; assistant re-encoding and V4
assertion/selection/model sidecars cannot manufacture coordinates. Ordinary
prose, omitted qualifiers, unsupported units/times and partial records abstain.
Identities and intervals are exact literals. No alias/unit/time conversion or
semantic overlap comparison occurs here. Numeric record and condition values
are bounded using their original tokens, before float conversion. They qualify
only if the existing canonical JSON encoder preserves the original numerical
meaning; underflow/rounded tokens abstain. Original bytes, hashes and spans are
unchanged, and ordinary representable numbers, booleans and Unicode stay distinct.

The separate [native local final-use purpose](native-claim-use-contests.md#native-local-final-use)
can now record exact whole-final adoption after ordinary batch completion. The
same-window claim/contest join is also documented there. Neither changes v1
publication lifetime or grants egress. The original-output prerequisite below
joins native original emission to adoption. Still open: mandatory-answer
relevance, post-original evidence ingress, adopted-claim contest/correction
joins, and once-only correction delivery. This is not full F22 completion.

## Native original-output receipts (local prerequisite)

`agent/supervision_original_output.py` installs one private native observation
lease **before each API attempt**, after flushing the previous stream tail.
It requires exactly one eligible native final-use selection and this separate
profile-local metadata policy (off by default):

```yaml
supervision:
  enabled: true
  original_output:
    version: supervision.original-output.v1
    enabled: true
```

The existing literal-source registration and `literal_sources.final_use` grant
are still required; this policy does not grant either, enable an external
supervisor, authorize provider disclosure, or permit correction delivery.
This is a configuration contract, not live activation clearance.

An early native observation is **not adoption**. The ordinary accepted-final
consumer commits adoption, then the actual post-hook finalizer must retain the
exact whole rendering. Only those two facts plus a complete native sink
observation can set `original_emitted`. A hook rewrite, replacement final,
retry, extra streamed content, or superseded writer cannot retroactively adopt
matching text. Fragmented provider streams are not buffered or reconstructed;
the first complete matching native segment or the final response can carry
private origin identity. String copies, serialization and arbitrary transforms
lose that identity. Known native no-op sanitation/media-extraction transforms
preserve it only when the complete text is unchanged and no media is involved.

Supported native projections:

| Surface | Qualified output | Remains unknown (ordinary delivery unchanged) |
| --- | --- | --- |
| CLI | Final response panel, `display.final_response_markdown: strip` (the default) when stripping changes nothing, or `raw`; native Rich literal segments through prompt_toolkit PlainTextOutput/VT100 to a descriptor-backed UTF-8 stream. Ordered soft-wrapped lines qualify at normal terminal widths. | `render` Markdown, changed strip output, controls, truncation, unsupported renderers, queued stdout proxies, history replay, incomplete/failed line groups. |
| Telegram Gateway | One escape-only MarkdownV2 `send_message` to a forum `message_thread_id`, no reply anchor, unchanged bot and routing; ordinary sanitation, extraction and `send_final_ledgered` finalization. | Rich messages, splits, edits, root/private/direct-message-topic variants, reply anchors, altered formatting/routing, failed/cancelled/ambiguous sends. No rich-disable setting is required for plain content that naturally uses MarkdownV2. |
| TUI | Descriptor-backed stdio `message.delta` or `message.complete`, exact native JSON text field and current session/transport; real write and flush. | WebSocket/enqueue acknowledgments, alternate `rendered` fields, detached/replaced transports, short writes, failed writes/flushes, oversized frames. This proves backend emission, not client rendering or user reading. |

Rich wrapping is proven from the renderer's marked segments, including exact
source offsets and space elisions, before writes. Each native line is observed
separately; only a complete ordered group on one origin/transport qualifies.
At most 32 compact line receipts are retained, without buffering output bodies.
The renderer remains capped at 16,384 characters, each native payload at 16 KiB,
and the existing 16 KiB
canonical record cap can refuse large receipt groups. Partial groups never
produce a whole-original receipt. The observation's `segments` holds each line's
actual payload hash/byte count; the top-level payload fields identify its final
line, not a fabricated concatenated transport write.

Facts persist as `kind=claim_delivery`, `dimension=original_output` in the existing
`supervision_owner_records` family, separate from the declaration/adoption row.
They retain exact source/adoption/attempt and sink hashes/identities, not source
bodies. There is no new DDL, generic callback setter, correction queue, transcript
rewrite, send retry or second ledger. Canonical writes recheck the installed
lease, current runtime/session/DB identity, exact predecessor bodies, declaration
or adoption reference and current sink under the existing fences. Cross-thread
callbacks use a nonblocking runtime lock and zero-wait SQLite writer: contention,
deletion, reset, revocation or persistence failure leave delivery unknown without
changing ordinary output. Automatic retention preserves unresolved original
records; explicit session deletion/reset remains authoritative. Restart readback
recovers historical facts only, never origin capabilities or replay authority.

## Verification workflow

Run `tests/agent/test_supervision_literal_sources.py` through the canonical
`scripts/run_tests.sh --file-retries 0`, with the matching LCM checkout importable
as `hermes_lcm`, isolated HOME/HERMES_HOME/TMPDIR/SQLite, and network/DNS denied.
The suite starts at the real plugin entrypoint and real context-engine clone,
uses ordinary tool dispatch and real SQLite rows, and forbids network/provider
scheduling and additional retrieval. Missing LCM is an explicit skip, not proof.
Also run `test_supervision_literal_source_fences.py` and
`test_supervision_literal_source_integrity.py`, plus LCM's literal-number,
literal-record, evidence-pack/compiler and evidence-contract tests.
Never substitute handwritten `SourcePropositionV1` fixtures for producer proof.
