# Native emitted-payload observation (F22 prerequisite)

This is a private, provider-neutral **sink observation seam**, not a completed F22
intervention. It does not establish claim adoption, source authority, consequence,
truth, renderer display, human reading, correction delivery, or durable receipt
storage. No new plugin grant, public RPC field, client ACK, database, destination,
model call, output replay, or global stream buffer is introduced.

## Native installation and transition API

The native claim/delivery owner may call
`agent.native_emission.install_for_runtime(runtime, callback)` while the existing
`SupervisionRuntime` owns a current `_current_turn_id`. It uses `_assert_owner`,
requires the actual agent/runtime association, derives the identity from the
native revision, and replaces/closes any earlier lease. It is deliberately **not**
a `PluginContext`/supervision-facade grant. Merely registering `observe` does not
install it. No installation is activated by this change.

The installer returns `NativeEmissionScope`; `close()` withdraws it. Normal CLI
response panels, rendered streaming lines, quiet single-query printing, attached
stream-JSON emitters, TUI `_emit`, Gateway final delivery and native Gateway stream
consumers activate the installed lease themselves. Gateway final lookup uses the
adapter's existing routing-index `peek_session_id`, then the existing runtime
registry; it never matches by chat name or guesses an agent. Gateway stream runner
supplies its real `ctx.agent_holder` getter, resolved at each delivery tick.

`NativeEmissionScope(identity, callback, current=...)` plus `activate()` is the
lower-level private embedding/native-transport seam. It is not an authority token
for a plugin, source, claim or database record. Tests additionally exercise this
seam with normal sink callers and owned loopback transports.

The synchronous callback receives one frozen `EmittedPayload`:

- frozen `EmissionIdentity(profile, lineage, work_id, run_generation, turn_id)`;
- unique observation ID, surface, exact effective target key/value tuple;
- weak-object-lifetime transport generation (not a reusable Python address);
- native operation/format and message ID or frame identity;
- exact bounded payload bytes, SHA-256, and UTF-8 byte interval `[start,end)`;
- for Telegram split owners: batch identity, index/count and pre-attempt segment
  hash, separate from the **actual** post-fallback payload hash.

A callback is made only after the native success boundary. Callback exceptions
are swallowed without changing delivery or initiating a retry. Consumers must be
native, synchronous, nonblocking and do no inference/network/retrieval. Each lease
reserves at most 256 observations, each at most 16 KiB. Oversize, non-UTF8, unknown,
failed, ambiguous and over-cap payloads produce no receipt; they are never
truncated into purported complete evidence. There is no retained receipt history
in this module. Existing native queues keep their existing limits and policies.

`exact_emitted_span(receipt, literal_bytes, identity=..., target=...,
generation=...)` returns a unique exact byte span or `None`; transformed, repeated,
wrong-target/wrong-generation and cross-segment literals abstain.
`complete_exact_batch(receipts)` accepts only every unique ordinal of the same
unchanged split manifest on the same exact destination/transport. A fallback
transformation or destination change is not silently equated to the source text.
A resumed tail is a **new** batch and cannot retroactively certify a failed batch.
Overlapping literal occurrences are ambiguous too (`aa` in `aaa`). Transport
lifetimes use weak object identity, not SDK equality by remote account: replacing
a Telegram bot for the same account still gets a different generation.
IDs, `SendResult.success`, `partial_overflow`, `_mark_final_delivered`, transcript
persistence and queued `write(True)` never certify payload completeness here.

Ordinary `finish_turn()` seals reasoning **before** final rendering, so it does
not invalidate the selected emission lease. Reset/revoke, instruction/revision
changes, runtime replacement and a new turn ID do. Cancellation of WeCom queued
calls, Gateway run-current fences, TUI detach/reattach generations, closed sockets
and explicit scope closure veto late callbacks. The parent canonical owner must
still revalidate its authority atomically when persisting a callback.

## Actual source-to-sink traces

| Surface | Ordinary producer → actual success boundary |
| --- | --- |
| CLI panel | `_chat_print_response_panel` → Rich `ChatConsole` render/OSC cleanup → `_cprint` (possibly scheduled) → local native prompt_toolkit output copy → actual `TextIO.write` **and** `flush` |
| CLI streaming | `_stream_delta` / `_flush_stream` → markdown/table transformations → `_emit_stream_line` → same actual rendered output boundary; held partial lines are not body emissions |
| CLI single query | `_run_quiet_single_query` selects the final/follow-up response → stdout print → actual write/flush; no receipt from agent return |
| CLI JSON | `StreamJsonEmitter.attach` / deltas / `emit_result` → `_emit` serializes timestamped JSONL → actual stdout write/flush; swallowed BrokenPipe/OSError produces no callback |
| Gateway final | `send_final_ledgered` → normal retry owner → Telegram rich or MarkdownV2 split/format/fallback owners → native bot send/edit completion; callbacks retain effective chat/topic/reply routing and returned message IDs |
| Gateway streaming | runner installs its actual agent getter → consumer normal `run`, final/edit/commentary/overflow paths → Telegram send/edit sinks; no callback from consumer delivery flags |
| Gateway native frame | WeCom `send_stream_frame` → normal per-chat queue (private scope carried beside work) → `_finalize_turn` → `_send_stream_reply` → actual socket send + existing correlated successful platform ACK; exact truncated/ZWSP-bearing final text. Preview-only, queued, timeout-assumed-delivered and errcode 6000 are not receipts |
| TUI stdio | `prompt_turn` → `_emit(message.complete)` / streamed events → `write_json` stamps frame → actual stdio write/flush; muted `_emit`, short writes and disabled flush abstain |
| TUI WS/fanout | `_emit` → optional existing per-peer fanout mailbox → serialized/sanitized frame → per-peer `_safe_send_many` completed `send_text`; private context and attachment fence travel **outside** JSON. Coalescing/enqueue/slow-write true are not callbacks; reconnect gets a new transport generation |
| Headless | `AIAgent.chat` / final response return / canonical transcript persistence are deliberately not sinks |

CLI prompt_toolkit observation is qualified for native PlainTextOutput and VT100
outputs backed by `io.TextIOBase`, with no preexisting output buffer. Unknown,
queue-like stdout proxies, custom/Win32 renderers and already-buffered output
remain unobserved rather than claiming a flush from a print return. Audio-only
TTS has no text receipt; merely closing an already-streamed box does not certify
its body. TTS text outside the qualified renderer scope is not retrospectively
adopted.

TUI stdio requires a native `io.TextIOBase` stream; queue-like/custom proxies stay
unknown even if their write/flush return normally. Serialization-error reply
frames do not inherit the failed assistant event's observation. WebSocket
completion also checks the unchanged native send deadline: a socket coroutine
that suppresses deadline cancellation cannot certify its late output. This does
not change the baseline transport's timeout, return value or retry policy.

Unknown, failed, timed-out or undrained predecessor ACKs remain unknown even if a
later success uses the same WeCom `req_id`: the protocol cannot distinguish that
late preview ACK from the final frame's ACK. Baseline delivery behavior is unchanged.

Gateway native payload producers implemented here are **Telegram text/rich
send/edit/splits and WeCom native final frames**. Other adapters (including relay,
remote proxy-only owners, custom platform plugins, media-only sends and WeCom
standalone markdown) remain unknown until their actual formatting/transport
owners implement a corresponding source-bound native completion seam. A generic
adapter `success=True` is intentionally insufficient. Telegram rich bytes encode
the exact native `rich_message` object using the documented `native-rich-json`
codec; they are not a claim about HTTP framing. No universal all-adapter closure
is asserted.

## Parent join still required

The existing `supervision_receipts.record/persist` is proposal/source-token bound
and is **not** semantically suitable for original unproposed output. This lane
does not fabricate a Jev proposal or source-adoption token and does not write new
canonical DDL. The planning lane owns canonical owner-record schema changes.

Parent integration must:

1. Select the native observation lease for the real turn before the relevant
   output, using actual adopted claim/source records and native owner policy.
2. Join immutable source semantics, main-agent whole-record rendering, exact
   emitted spans and current mandatory answer dependencies. Observation alone
   does not authorize or prove this join.
3. Persist selected → emitted/unknown transitions through the canonical writer
   and the actual registration/source tokens, after revalidating scope,
   destination, generation, payload and all required segment coverage. A crash
   between sending and receipt persistence stays unknown; do not replay it.
4. Implement correction policy, incident deduplication, currentness and its own
   actual correction-emission receipt; qualify busy/rollback, retention/deletion,
   restart and late-result behavior at that durable join.

Until those steps are qualified, these callbacks are only the emission
prerequisite. They are not completed F22 effects and do not close all22/tracker
items.
