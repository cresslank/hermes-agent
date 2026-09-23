# Native retrieval presentation v1

`supervision.retrieval-presentation.v1` is an optional, additive **output**
contract, separate from `supervision.owner-consumption.v1`, source grants and
inference dispatch admission. It does not change disclosure policy or grant
source access. It makes no security-sandbox or truth-winner claim.

## Negotiation and ownership

* The host facade advertises `retrieval_presentation` with this exact version.
* LCM explicitly requests `output_contract` outside its disclosed `facts`; the
  owner codec echoes it only for the matching rank request. Older hosts and
  unrecognized versions cannot optimistically acknowledge annotations.
* Muxyard requests it only where a final tool renderer is installed: generic
  `web_extract` and multi-source `muxyard_extract`. Direct provider list APIs
  remain order-only. The dedicated tool preserves its existing F16 window path;
  the generic tool retains its existing missing-full-source abstention.
* Typed MCP passes the version to each individually authorized source adapter;
  the adapter must explicitly return it. Existing adapters returning the old
  four-field projection remain order-only. The Switchloom adapter opts in only
  to an exact supported version. Recipient, route, acquired session, source,
  account, revision and one-use inference-admission fences are unchanged.

No new field is inserted into the inference projection, no egress grant is
widened, and no additional retrieval/model call is made.

## Actual returned JSON

The final JSON document gains a `retrieval_presentation` object only when there
is a real annotation effect. It contains the version, `sources_remain_untrusted`
and one or both distinct `conflicts` / `lower_trust_isolation` blocks. Each block
has a fixed owner-written notice and a bounded list of `{source_pointer, ref}`
entries. Pointers resolve within **that returned JSON document** to unchanged
original source rows. They are not free-form provider prose. Conflict and
isolation may overlap. No source is deleted, rewritten, promoted to instructions,
or certified trustworthy by absence of an annotation.

LCM points into final `hits`; Muxyard into final `results`; MCP into the existing
structured result inside its native rendered envelope. Tail, scores, exact refs,
qualifiers, original content and completeness/error metadata are preserved.
LCM refuses annotations if final hydration/diversity shaping loses any baseline
row or a flagged row's complete source text. A flag never stands in for missing
source bytes. Existing order-only recall diversity behavior is unchanged.

An unchanged permutation without an actual block is baseline/no-op. A negotiated
annotation-only result is now a real effect; unsupported annotation-only owners
still return their original baseline. Foreign, malformed and duplicate IDs,
output-field collisions, lost qualifiers, serialization/cap failures and stale
source/currentness/grants veto consumption. Both blocks are validated even on
order-only paths. LCM retains its response character cap, Mux its 131072-character
final-view cap, MCP its 262144-byte cap. None is increased.

## Settlement

LCM, generic/dedicated Mux tool renderers and typed MCP hash the exact final UTF-8
string returned to the tool caller. They do not hash an intermediate list or a
canonical re-encoding of that string. A final rejected acknowledgement restores
the saved baseline. No source/transport grant is renewed by successful shaping.
Direct Mux provider list APIs retain their old order-only canonical-object
receipt; this contract makes no final tool-byte claim for those list APIs.

## Premise and verification limits

Current ordinary query-only owner producers do **not** infer a factual premise
from a question or source text. They therefore do not ask conflict questions
without an explicit qualified owner premise. Native conflict-renderer tests use
an explicitly labeled synthetic owner-premise projection and the real registered
plugin, inference codec, native readers and final response. Ordinary isolation,
reorder and absence/no-op tests require no premise supplementation. This is not
proof of an ordinary premise supplier, nor of live-provider classification
quality. Unknown/incomplete qualifier and premise cases remain abstentions.

Regression tests retain the old unsupported-owner C03 negative controls and add
negotiated positive controls. C01/C02 original revision, recipient/transport and
one-use dispatch contracts are not replaced. This is a local F14 presentation
change, not F14/all-feature acceptance, activation or publication.
