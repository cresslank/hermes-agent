# Native retrieval working premises

`supervision.working-premise.v1` is an optional, separately authorized use of a
native LCM whole literal record. It supplies F14's factual comparison context;
it does **not** certify truth, accept a final claim, or trigger F22 adoption or
contest. Unstructured prose and unsupported owners remain query-only.

## Ordinary producer and consumer

1. The main agent reads an original user/tool `lcm.literal-record.v1` through
   ordinary LCM tools and commits its exact reversible `LCM record <ref>: <JSON
   string>` rendering in a requested artifact.
2. The existing committed work-map and `claim_use` / `answer_dependency` annex
   link that entire claim to an authenticated accepted requirement. See
   [native claim-use contests](native-claim-use-contests.md); the node remains
   `claim_declared`, not adopted or delivered.
3. An ordinary `lcm_recall` uses the **exact linked requirement span** as its
   query, including list markers/qualifiers. The query is only a linkage key,
   never the factual premise. Exactly one current linked working use must match.
4. The actual current retrieval batch must contain that source and consist of
   complete, in-scope, comparable whole literal records (same entity, predicate,
   interval, scope/conditions and quantity/unit). All selected rows must belong
   to the active native session, including when recall searched all sessions.
   Missing/ambiguous links or any incompatible/malformed/clipped source abstain.
5. Common owner admission resolves these already-selected exact refs through
   the registered native source provider, outside host authority/write locks.
   It adds the original complete source bytes as `facts.premise` for the single
   authorized recipient. The real F14 factory compares each named passage;
   source bodies and metadata remain intact in shared state instead of being
   duplicated in every question. The conflict questions include the exact premise.
6. The existing native final renderer retains every baseline row and adds
   document-local conflict pointers. This is potential incompatibility, not a
   truth winner or deletion. Final ACK rechecks the original source/use/grants,
   native lifecycle and revision, and records the digest of the returned UTF-8.

The original shared request deadline bounds preparation, send admission and
final ACK. The supplier does not publish or renew `literal:` handles. Previous
`supervision.literal-sources.v1` publication lookup and expiry stay unchanged.
There is no additional search, model turn, assertion index or parallel database.

## Independent source and egress grants

Alongside the existing native literal-source registration policy, the LCM owner
needs this **additional** profile policy (illustrative, not activation advice):

```yaml
working_premises:
  version: supervision.working-premise.v1
  recipients: [example-supervisor]
```

The matching recipient needs `observe`, `rank_candidates`, and local data classes
`history_excerpt`, `project_excerpt`, `task_text`. Its existing per-field egress
policy must explicitly classify `premise` and map `sources.premise` to
`supervision.working-premise.v1`. The judgment plugin must independently allow
that class and, for a private class, that source grant. All ordinary candidate,
query and scope fields still require their own egress permissions. No grant is
inferred from the query, literal schema, model declaration, another recipient,
or any final-use/local F22 purpose.

The recipient-specific one-use send gate rereads native rows before acquiring
host fences, then checks working-use authority under the source lifecycle fence.
ACK does the same. Native point reads use the registered store connection (not
a reopened database pathname), refuse pending/contended writes, and temporarily
use a zero SQLite busy timeout. Source I/O is never performed while holding host
runtime, recipient or canonical graph-write fences. Missing source/native
capability or revocation restores query-only behavior before judgment, or the
exact native baseline after selection. No late advisory is retained.

## Boundaries and qualification

The working supplier currently applies only to native LCM recall. Generic and
dedicated Muxyard and typed MCP retain their existing query-only/reorder/isolation
behavior; they do not inherit LCM premise or remote-disclosure authority. No
cross-owner working-premise applicability is claimed.

Run `tests/agent/test_supervision_working_premise.py` using the canonical host
runner and matching isolated host/Jev/LCM checkouts. The suite uses real native
registration, committed artifacts, SQLite, recall, strict fake HTTP and actual
returned-byte receipts. It covers source/claim/scope/qualifier/egress failures,
pre-send revocation, final-ACK busy/revision/lifecycle/deadline vetoes, and
registered-connection identity after pathname replacement. Live judgment quality,
post-return mutation exclusion, broad performance and full F14/F22 closure are
separate claims.
