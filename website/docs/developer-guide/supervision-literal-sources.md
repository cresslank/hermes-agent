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
original shared deadline and active engine. Publication consumes it **once** and
resolves opaque source handles through the registered provider; it accepts no
record payload. A copied dataclass cannot publish or cancel it. The unchanged
public tool argument schemas receive no new keys; successful tool output may
include `source_propositions`, a list of opaque host refs. Copying that list
confers no authority.

The later native graph/adoption join can call:

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
registration then recipient. Registry membership is checked before taking the
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
- A returned proposition is a snapshot, not a perpetual capability. Every actual
  consuming effect must reenter lookup. No post-return concurrent-mutation
  exclusion is promised. Parent joins must perform their final source check at
  their effect boundary and explicitly design any later durable adoption or
  fresh-invocation renewal; copying an expired source proposition is insufficient.

## Grammar boundary and remaining joins

`lcm.literal-record.v1` is documented in LCM's `docs/literal-record.md`. Only
complete original user/tool rows qualify; assistant re-encoding and V4
assertion/selection/model sidecars cannot manufacture coordinates. Ordinary
prose, omitted qualifiers, unsupported units/times and partial records abstain.
Identities and intervals are exact literals. No alias/unit/time conversion or
semantic overlap comparison occurs here.

Still open: main-agent adoption of an exact reversible whole-record rendering;
canonical claim persistence and dependency graph joins; exact-coordinate pair
eligibility; negotiated F22 contest/correction actions; consequential required
answer dependency policy; actual original/correction emission and durable
receipts. Source publication alone does not close any of these requirements.

## Verification workflow

Run `tests/agent/test_supervision_literal_sources.py` through the canonical
`scripts/run_tests.sh --file-retries 0`, with the matching LCM checkout importable
as `hermes_lcm`, isolated HOME/HERMES_HOME/TMPDIR/SQLite, and network/DNS denied.
The suite starts at the real plugin entrypoint and real context-engine clone,
uses ordinary tool dispatch and real SQLite rows, and forbids network/provider
scheduling and additional retrieval. Missing LCM is an explicit skip, not proof.
Also run LCM's literal-record, evidence-pack/compiler and evidence-contract tests.
Never substitute handwritten `SourcePropositionV1` fixtures for producer proof.
