"""Native ORIGINAL output facts, independent of candidate adoption/source grants.

One attempt record in the existing canonical owner family, never a second ledger.
An observation may precede adoption; only this attempt's post-hook final can join
it. No source I/O, inference, send, retry or correction is performed by callbacks.
"""
from dataclasses import dataclass
import copy
import hashlib
import json
import logging
import uuid

from agent import supervision_planning_records as records
from agent.native_emission import EmittedPayload, install_for_runtime
from agent.supervision_context import digest
from agent.supervision_final_use import selection_scope
from agent.supervision_claim_uses import render_record

logger = logging.getLogger(__name__)
POLICY = {"version": "supervision.original-output.v1", "enabled": True}


def valid_policy(value):
    return type(value) is dict and value == POLICY and value.get("enabled") is True


def fingerprint(row):
    return digest(json.dumps(row, sort_keys=True, separators=(",", ":"), allow_nan=False))


@dataclass(frozen=True, eq=False)
class _Origin:
    owner: object
    stage: str
    text: str
    id: str


class _OriginalText(str):
    """Private provenance travels with native text, never serialized authority.

    Only exact no-op whitespace stripping preserves identity. Other transforms
    drop provenance unless their native formatting owner supplies a codec proof.
    """
    def __new__(cls, origin):
        value = super().__new__(cls, origin.text)
        value._origin = origin
        return value

    def __copy__(self):
        return str(self)

    def __deepcopy__(self, memo):
        return str(self)

    def __reduce_ex__(self, protocol):
        return str, (str(self),)

    def strip(self, chars=None):
        result = super().strip(chars)
        return self if result == self else result


@dataclass(frozen=True)
class _Codec:
    origin: _Origin
    name: str
    wire_hash: str
    mapping: tuple


def native_text_sink(stream):
    """A memory buffer or queue has no native output descriptor."""
    import io
    if not isinstance(stream, io.TextIOBase) or stream.closed:
        return False
    try:
        descriptor = stream.fileno()
        return type(descriptor) is int and descriptor >= 0
    except (AttributeError, OSError, ValueError):
        return False


def origin_of(text):
    if type(text) is _OriginalText:
        origin = text._origin
        if (type(origin) is _Origin and origin.owner.issued.get(origin.id) is origin
                and str(text) == origin.text):
            return origin
    return None


def unchanged(original, rendered):
    """A native transform can retain existing provenance only for a whole no-op."""
    return original if origin_of(original) is not None and rendered == original else rendered


def codec(text, payload, name, mapping):
    """Called by a native formatting owner after proving its lossless projection."""
    origin = origin_of(text)
    if origin is None or not isinstance(payload, str):
        return None
    return _Codec(origin, name, hashlib.sha256(payload.encode()).hexdigest(), tuple(mapping))


class _Attempt:
    def __init__(self, rt, selected, policy):
        self.rt = rt
        self.policy = policy
        self.revision = rt.revision
        self.database = getattr(rt.agent(), "_session_db", None)
        self.declared = copy.deepcopy(selected.node)
        self.claim = self.declared
        self.expected = render_record(selected.source.record)
        self.issued = {}
        self.cli_segments = []
        self.stream_seen = False
        self.stream_extra = False
        self.writer = None
        self.final = None
        self.scope = None
        self.row = dict(id="original:" + uuid.uuid4().hex, kind="claim_delivery",
            dimension="original_output", revision=1, status="original_pending",
            claim_record=self.claim["id"], declaration_hash=fingerprint(self.claim),
            declaration_revision=self.claim["revision"], claim_id=self.claim["claim_id"],
            source=rt.dependencies.claim_uses._identity(selected.source),
            selected_invocation=selected.source.invocation_id, render_hash=digest(self.expected),
            adoption=None, accepted_final=None, observation=None)

    def current(self):
        rt = self.rt
        return (getattr(rt, "_original_output_attempt", None) is self
            and valid_policy(self.policy) and rt.revision == self.revision
            and (self.writer is None or getattr(rt.agent(), "_stream_writer_token", None) == self.writer)
            and getattr(rt.agent(), "_session_db", None) is self.database
            and self.scope is not None and rt._native_emission_scope is self.scope
            and self.scope.current())

    def _write(self, row, *, initial=False, sink_current=None):
        # This private path is callable only on this runtime's installed native
        # attempt, not a generic cross-thread setter or tool-worker impersonation.
        if not self.current():
            return False
        predecessor = None if initial else {self.row["id"]: self.row}
        def validate():
            return self.current() and (sink_current is None or sink_current())
        if records.save_original(self, row, validate=validate,
                predecessors=None if initial else {self.row["id"]: (self.row["revision"], self.row["status"])},
                predecessor_records=predecessor, references={self.claim["id"]: self.claim}):
            self.row = row
            return True
        return False

    def issue(self, text, stage):
        if not self.current() or text != self.expected or len(self.issued) >= 4:
            return text
        origin = _Origin(self, stage, str(text), uuid.uuid4().hex)
        self.issued[origin.id] = origin
        return _OriginalText(origin)

    def stream(self, text):
        # First complete native segment only. Fragmented streams remain unknown;
        # we neither buffer nor find a literal inside arbitrary status/history.
        with self.rt.lock:
            agent = self.rt.agent()
            token = getattr(getattr(agent, "_stream_writer_tls", None), "token", None)
            if type(token) is not int or token <= 0 or token != getattr(agent, "_stream_writer_token", None):
                return text
            if self.writer is None:
                self.writer = token
            if self.stream_seen:
                self.stream_extra = self.stream_extra or bool(text)
                return text
            self.stream_seen = True
            return self.issue(text, "attempt")

    def adopted(self, row):
        with self.rt.lock:
            use = row.get("final_use", {})
            if (self.current() and row["id"] == self.declared["id"]
                    and row["revision"] == self.declared["revision"] + 1
                    and row["status"] == "adopted" and use.get("final_hash") == digest(self.expected)
                    and use.get("selected_invocation") == self.row["selected_invocation"]):
                self.claim = copy.deepcopy(row)
                updated = {**self.row, "revision": self.row["revision"] + 1,
                    "adoption": dict(record_id=row["id"], revision=row["revision"], body_hash=fingerprint(row))}
                self._write(updated)

    def finalized(self, text):
        with self.rt.lock:
            if not self.current() or self.row["adoption"] is None or text != self.expected:
                return text
            # Full equality at the actual post-hook boundary, never substring
            # adoption. Candidate-use metadata itself remains historically true.
            updated = {**self.row, "revision": self.row["revision"] + 1,
                "accepted_final": dict(hash=digest(text), span=[0, len(text)], attempt=self.row["id"])}
            if updated["observation"] is not None:
                if self.stream_extra and updated["observation"]["origin_stage"] == "attempt":
                    return text
                updated["status"] = "original_emitted"
            if not self._write(updated):
                return text
            self.final = self.issue(text, "final")
            return self.final

    def observed(self, receipt):
        # Called by the installed lease from actual CLI/Gateway/TUI callback
        # threads. Metadata is already bounded by the native sink, no source read.
        if type(receipt) is not EmittedPayload or type(receipt.origin) is not _Codec:
            return
        proof = receipt.origin
        origin = proof.origin
        if (origin.owner is not self or self.issued.get(origin.id) is not origin
                or proof.wire_hash != receipt.payload_sha256
                or hashlib.sha256(receipt.payload).hexdigest() != proof.wire_hash
                or receipt.segment_count != 1 or receipt.identity != self.scope.identity):
            return
        codecs = {"cli.literal-line.v1": "cli.rendered", "telegram.markdown-escape.v1": "gateway.telegram",
                  "tui.stdio-text.v1": "tui"}
        if codecs.get(proof.name) != receipt.surface:
            return
        # Never block an output thread on a graph owner. Busy -> unknown.
        if not self.rt.lock.acquire(blocking=False):
            return
        try:
            if not self.current() or self.row["observation"] is not None:
                return
            observation = dict(id=receipt.observation_id, payload_hash=receipt.payload_sha256,
                payload_bytes=len(receipt.payload), surface=receipt.surface, target=list(receipt.target),
                transport_generation=receipt.transport_generation, operation=receipt.operation,
                message_or_frame_id=receipt.message_or_frame_id, batch_id=receipt.batch_id,
                segment_index=receipt.segment_index, segment_count=receipt.segment_count,
                origin_id=origin.id, origin_stage=origin.stage, codec=proof.name,
                mapping=list(proof.mapping), source_bytes=len(origin.text.encode()),
                source_hash=digest(origin.text), ack_kind=receipt.ack_kind)
            if proof.name == "cli.literal-line.v1":
                _, group, index, count, _, _, prior, _, stop = proof.mapping
                parts = self.cli_segments
                if (not 0 < count <= 32 or index != len(parts)
                        or (parts and (parts[0][0] != group or parts[-1][1] != prior
                            or parts[0][2]["transport_generation"] != receipt.transport_generation
                            or parts[0][2]["origin_id"] != origin.id))):
                    return
                if not self.current() or not receipt.current():
                    return
                parts.append((group, stop, observation, receipt.current))
                if len(parts) != count or stop != len(origin.text):
                    return
                observation = {**observation, "segments": [part[2] for part in parts]}
                sink_current = lambda: all(part[3]() for part in parts)
            else:
                sink_current = receipt.current
            updated = {**self.row, "revision": self.row["revision"] + 1,
                "status": "original_emitted" if self.row["accepted_final"] is not None else "original_observed",
                "observation": observation}
            self._write(updated, sink_current=sink_current)
        finally:
            self.rt.lock.release()


def begin_attempt(agent):
    """Ordinary request owner, after old-stream tail flush and BEFORE dispatch."""
    try:
        from agent.supervision_policy import runtime_for_agent
        from hermes_cli.config import load_config_readonly
        rt = runtime_for_agent(agent)
        if rt is None:
            return
        rt._assert_owner()
        with rt.lock:
            old = getattr(rt, "_native_emission_scope", None)
            if old is not None:
                old.close()
            rt._original_output_attempt = None
        config = (load_config_readonly() or {}).get("supervision", {})
        policy = config.get("original_output") if type(config) is dict and config.get("enabled") is True else None
        if not valid_policy(policy):
            return
        with rt.lock:
            uses = rt.dependencies.claim_uses
            candidates = [s for owner in uses.final_sources for s in owner.selections.values()
                if s.runtime() is rt and s.scope == selection_scope(rt, s.node)
                and owner.current(rt) and uses._eligible(s.node, s.source)]
            if len(candidates) != 1:
                return
            attempt = _Attempt(rt, candidates[0], copy.deepcopy(policy))
            rt._original_output_attempt = attempt
            attempt.scope = install_for_runtime(rt, attempt.observed)
            if not attempt._write(attempt.row, initial=True):
                attempt.scope.close()
    except Exception:
        logger.debug("optional original-output attempt unavailable", exc_info=True)


def adopted(agent, row):
    rt = getattr(agent, "_supervision_runtime", None)
    attempt = getattr(rt, "_original_output_attempt", None)
    if type(attempt) is _Attempt:
        attempt.adopted(row)


def finalized(agent, text, *, eligible):
    try:
        rt = getattr(agent, "_supervision_runtime", None)
        attempt = getattr(rt, "_original_output_attempt", None)
        if type(attempt) is _Attempt and eligible:
            return attempt.finalized(text)
    except Exception:
        logger.debug("optional original-output final unavailable", exc_info=True)
    return text


def stream_text(agent, text):
    try:
        rt = getattr(agent, "_supervision_runtime", None)
        attempt = getattr(rt, "_original_output_attempt", None)
        if type(attempt) is _Attempt:
            return attempt.stream(text)
    except Exception:
        logger.debug("optional original-output stream unavailable", exc_info=True)
    return text
