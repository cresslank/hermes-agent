# Native literal claim-use contests (partial F22)

This is a bounded **claim-to-contest** join, not a claim-to-correction delivery
implementation. It changes optional dependency reuse; it does not change history,
certify source truth, wake a model, or deliver a message. F22 and full-feature
qualification remain open.

## Ordinary producer and owner effect

1. An already-requested main-agent plan commit contains the unchanged
   `hermes-work-map-v1` claim/requirement links and a separate
   `hermes-planning-proposals-v1` annex.
2. The annex may contain a closed record with exactly these keys:

   ```json
   {
     "type": "claim_use",
     "local_id": "answer-mass",
     "use": "answer_dependency",
     "claim_ref": {
       "source_ref": "claim.md",
       "source_revision": "<exact SHA-256 of the committed claim artifact>",
       "start": 0,
       "end": 500
     },
     "source_ref": "lcm:123:0-400",
     "requirement_refs": [
       {
         "source_ref": "<authenticated source-message ID>",
         "source_revision": "<exact SHA-256 of that source message>",
         "start": 0,
         "end": 100
       }
     ]
   }
   ```

   The example offsets and hashes are placeholders, not usable refs. Every span
   must resolve exactly; requirement spans must be current complete enumerated
   clauses and must match the claim's work-map links. A child write, unrequested
   artifact, unknown key, incidental citation, or unsupported `use` cannot declare
   adoption. Existing `todo` results expose authentic requirement coordinates.
3. The canonical owner stores `claim_declared`, **not adopted or delivered**.
   `source_ref` is an untrusted exact LCM source locator supplied from ordinary
   retrieval. It grants neither row access nor remote processing.
4. A later, already-requested ordinary `lcm_evidence_pack` or
   `lcm_compile_evidence` invocation hydrates the original whole JSON sources.
   The existing native publication supplies fresh `SourcePropositionV1` refs.
   This join reads only those published rows and does not request an expansion.
   It never renews an old literal-source grant: ordinary batch revision and
   deadline invalidation still apply unchanged.
5. Within that existing admission window, the dependency owner resolves the
   declaration to the current proposition and requires a reversible whole-source
   rendering. The **entire dedicated claim artifact** must equal:

   ```python
   "LCM record " + record.exact_ref + ": " + json.dumps(
       record.source_bytes.decode("utf-8"), ensure_ascii=True)
   ```

   All original JSON bytes, whitespace and qualifiers survive that string codec.
   The rendering must fit the existing 2,400-character claim bound. Paraphrases,
   quotes, surrounding rejection/context, duplicate occurrences, and incomplete
   records abstain. This intentionally narrow format is not a general natural
   language adoption detector.
6. Exactly one comparable pair and at most four declared affected claims qualify.
   Entity **and predicate**, event/atemporal interval, scope/conditions and
   quantity/unit pins must be identical. Values, polarity and modality remain in
   the complete source windows for classification. Ambiguous multiple pairs or
   affected-scope overflow abstain without a truncated completeness claim.
7. The real standalone bridge negotiates `supervision.claim-contest.v1`, with the
   literal mapping `contest_dependent_claim -> contest_dependent_claim`. It is
   neither F17 nor an alias for continuation. The profile must grant the action,
   observation and all three relevant data classes; exactly one active source-
   authorized recipient must qualify. Existing field-level egress and dispatch
   admission remain mandatory. Source/lifecycle/recipient grants are checked
   again at the one-use send boundary.
8. A valid incompatible proposal is revalidated at consumption, including fresh
   point reads outside graph/SQLite locks. The existing canonical owner-record
   writer compares predecessor revision **and status**, then rechecks current
   source/lifecycle grants, graph bindings, target, revision and original deadline
   inside its transaction. It records `contested`, both exact native refs and their
   source identities, `truth_winner=null`, and `suspend_optional_reuse=true`.
   `DependencyOwner.optional_support` refuses every affected claim, including
   after the existing graph reload. Required obligations remain unchanged.

There is no new graph/database, model-visible tool, work-map key, source setter,
clock policy, inference retry, or destination. The shared 64-record bound and
canonical deletion/reset/retention owner still apply. A durable contested row is
unresolved and is not aged out as an ordinary terminal receipt. Busy/unavailable
storage never creates a successful contest receipt. Immutable pair/claim identity
also prevents repeated same-runtime no-op classification after fresh publication;
durable contested rows prevent replay after graph reload.

## Native-local final use

This is a separate opt-in purpose under the **existing** literal source owner:

```yaml
supervision:
  enabled: true
  plugins:
    hermes-lcm:
      literal_sources:
        version: supervision.literal-sources.v1
        recipients: [example-supervisor]
        final_use:
          version: supervision.literal-final-use.v1
          consumer: native:accepted-final
          enabled: true
```

This is a contract example, not activation. The `final_use` mapping is closed:
exactly those three keys/values are required (including literal boolean `true`).
Absent, disabled, malformed, old-version or other-consumer policy denies local
final use. The current registered policy is scoped to its owner registration;
policy reload/replacement closes the old registration and loses all selections.
Existing observation/history grants do **not** opt in. Old providers inherit
explicit denial. This purpose has no public model tool, arbitrary recipient,
row-ID lookup, inference dispatch or remote disclosure authority.

During real pack/auto/proposal hydration, the host may enroll only an already
selected complete row uniquely linked to an eligible **committed** claim-use
node. The LCM owner retains an opaque, bounded in-memory selection, not a new
database, persisted source body or adoption receipt. It binds the actual engine,
store/home/database/session/lifecycle, registered native consumer, native work,
turn/instruction/requirement/catalog/control generations, and exact canonical
claim/declaration/artifact predecessor fingerprint. The shared 64-source bound
includes these selections and in-flight final permissions. Only ordinary
evidence-round bookkeeping may be crossed; no old publication is retained or
revived. `lookup_literal_source` still rejects after batch completion or expiry.

The real `finish_text_response` branch calls the native final-use consumer only
**after all stop/continuation gates**, including F20, accept the final. Neither
hydration nor `prepare_final` adopts. The entire untransformed visible final,
whole dedicated claim artifact and exact claim span must all equal the reversible
`LCM record <exact_ref>: <JSON string of all original source bytes>` form above.
This is the owner's positive record grammar in conjunction with the committed
`answer_dependency` declaration, not a substring detector. It does not interpret
negation inside arbitrary prose. Quotation wrappers, rejection, extra context,
repetition, paraphrase, transformed/think-only or joined-fragment final output
are unsupported. One final and one unambiguous declaration qualify; the existing
2,400-character rendered-claim bound remains unchanged.

At this accepted native phase LCM consumes its selection for **one fresh local
permission**, re-reading only that original row with a read-only, zero-busy-wait
SQLite connection. It compares whole bytes, span, numeric/other coordinate pins
and native attribution with the original validator. It neither invokes the old
publication resolver around a failed host lookup nor searches for another row.
Source I/O is outside graph, registration, provider and canonical write locks.
The synchronous native phase, not a renewed 150 ms or inference deadline, owns
this bounded point read and canonical write; cancellation or generation loss
makes the permission unavailable. No background worker, wait/retry budget or
hard OS filesystem-latency guarantee is introduced.

The host then fences the actual current final/engine/consumer/policy and LCM
lifecycle, and rechecks them inside the existing zero-wait canonical writer.
The writer additionally compares exact predecessor body, revision and status.
A missing/deleted/reset/retained-away or busy record cannot be recreated as
adopted. Only an acknowledged canonical write updates the in-memory graph;
failures are not retried. Successful `claim_declared -> adopted` stores the
accepted final hash/span/current revision, selection invocation provenance,
exact claim and declaration revision, and complete
source identity through the existing `claim_delivery` family. It never stores
source bodies, marks truth, grants finality, or proves emission or human receipt.
`emission` and `correction_relevance` remain `unknown`.

Final attempts consume their selections even on unavailability; accepted input,
turn end/cancel/rebind, lifecycle transitions, owner replacement and process exit
invalidate them. Restart cannot mint authority from a persisted `adopted` row.
Native source revalidation and the lifecycle fence do not promise exclusion of
arbitrary out-of-process SQLite/filesystem tampering after the final point read;
as with v1, no cross-database atomic source snapshot is claimed. This is not an
external presentation or source-truth attestation.

Qualify with `test_supervision_final_use.py` and the unchanged
`test_supervision_claim_final_boundary.py`, using the canonical host runner and
matching LCM source. Positive routes use real ordinary hydration, batch
finalization, accepted final owner and canonical readback, including actual
elapsed time past the original publication deadline. Negative controls retain
ordinary output and source-read success when optional callbacks fail.

## Explicitly unsupported paths

- `required_answer_dependency.v1` mandatory-answer/decision relevance is **not yet
  implemented**. `use=answer_dependency` is only a main-agent declaration, not
  proof of current mandatory consequence. Records retain
  `correction_relevance="unknown"` and native events never assert consequential
  delivery.
- No original-emission selection/receipt joins the native sink lease. Records
  retain `emission="unknown"`; no sink is claimed qualified by this change.
- `focused_correction` remains unsupported in native codec negotiation. There is
  no correction reservation, destination routing, separate emission receipt,
  uncertain-send recovery, or cross-sink delivery qualification.
- Literal source revocation vetoes this join and future source use. It does not
  independently issue a correction. Existing artifact-version invalidation and
  its deterministic `evidence_revoked` no-semantic-action path are unchanged.
- No cross-turn source authority, restart source-grant resurrection, automatic
  retrieval, generic prose adoption, all-pair search, or universal source-owner
  support is promised. Restart does not reconstruct vanished source stores.

See [native emitted-payload observation](native-emitted-payload-observation.md)
for the separately installed sink prerequisite. Its remaining parent joins are
still required; contest tests cannot establish external delivery or live readiness.
