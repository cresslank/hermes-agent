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
