"""Bounded canonical metadata receipts; never a second work or source authority.

Writes use an already initialized SessionDB path with zero busy wait. A selected
receipt records intent, not consumption. An interrupted selected effect is unknown
and is never automatically replayed as an external inference/tool operation.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
from pathlib import Path
import sqlite3
import time

from agent.supervision_types import project

MAX_WORK = 256
MAX_RECEIPTS = 4096
RETENTION_SECONDS = 7 * 24 * 3600
TERMINAL = ("applied", "consumed", "no_op", "rejected", "stale", "expired")
REASONS = frozenset("""queued owner_selection owner_advisory owner_settlement native_view
revoked revision_changed deadline grant_missing target_closed opportunity_missing
opportunity_deadline owner_action_mismatch unbound_evidence incident_settled
invalid_template invalid_candidates invalid_relation user_stop queue_full
critical_span_omitted empty_selection missing_relation continuation_budget
view_contract storage_unavailable owner_outcome generation_changed admission_selected
admission_persisted admission_consumed admission_parked admission_invalid
owner_selected owner_unacknowledged owner_postvalidation owner_ack_capacity expansion_contract""".split())


@contextmanager
def writer(runtime):
    agent = runtime.agent()
    db = getattr(agent, "_session_db", None)
    path = getattr(db, "db_path", None)
    if not isinstance(path, (str, Path)) or not Path(path).is_file():
        raise sqlite3.OperationalError("receipt_store_unavailable")
    # mode=rw prevents creation/resurrection. No schema replay or journal-mode change.
    conn = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=rw", uri=True, timeout=0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        session = getattr(agent, "session_id", "")
        if not conn.execute("SELECT 1 FROM sessions WHERE id=?", (session,)).fetchone():
            raise sqlite3.OperationalError("receipt_session_missing")
        if conn.execute("SELECT 1 FROM supervision_generations WHERE session_id=? AND revoked=1", (session,)).fetchone():
            raise sqlite3.OperationalError("receipt_generation_revoked")
        yield conn, session
        conn.commit()
    finally:
        conn.close()


def prune(conn, now=None):
    now = time.time() if now is None else now
    from agent.supervision_planning_records import prune as prune_owner_records
    prune_owner_records(conn, now)
    cutoff = now - RETENTION_SECONDS
    from agent.supervision_store import UNSETTLED_CONTROL_SQL
    conn.execute(f"""DELETE FROM supervision_receipts WHERE updated_at<?
        AND NOT EXISTS (SELECT 1 FROM delegation_controls c
            WHERE c.parent_session_id=supervision_receipts.session_id AND ({UNSETTLED_CONTROL_SQL}))
        AND status IN ('applied','consumed','no_op','rejected','stale','expired')
        AND (delivery_id IS NULL OR NOT EXISTS (SELECT 1 FROM supervision_admissions a
            WHERE a.delivery_id=supervision_receipts.delivery_id AND a.state!='consumed'))""", (cutoff,))
    conn.execute("""DELETE FROM supervision_work WHERE closed=1 AND updated_at<?
        AND NOT EXISTS (SELECT 1 FROM supervision_receipts r WHERE
          r.profile=supervision_work.profile AND r.work_id=supervision_work.work_id
          AND r.lineage=supervision_work.lineage)
        AND NOT EXISTS (SELECT 1 FROM supervision_owner_records o WHERE
          o.profile=supervision_work.profile AND o.work_id=supervision_work.work_id
          AND o.lineage=supervision_work.lineage)""", (cutoff,))


def _work(conn, runtime, session):
    rev = runtime.revision
    now = time.time()
    key = (rev.profile, rev.lineage, rev.work_id)
    if not conn.execute("SELECT 1 FROM supervision_work WHERE profile=? AND lineage=? AND work_id=?", key).fetchone():
        if conn.execute("SELECT count(*) FROM supervision_work WHERE profile=?", (rev.profile,)).fetchone()[0] >= MAX_WORK:
            raise sqlite3.OperationalError("receipt_capacity")
    conn.execute("""INSERT INTO supervision_work VALUES(?,?,?,?,?,?,?,?)
        ON CONFLICT(profile,lineage,work_id) DO UPDATE SET
        revision_json=excluded.revision_json,closed=excluded.closed,updated_at=excluded.updated_at""",
        (*key, session, json.dumps(project(rev)), int(runtime.closed), now, now))


def record_work(runtime):
    try:
        with writer(runtime) as (conn, session):
            _work(conn, runtime, session)
        return True
    except (OSError, sqlite3.Error):
        return False


def record(runtime, proposal, receipt, *, delivery_id=None):
    try:
        with writer(runtime) as (conn, session):
            _work(conn, runtime, session)
            persist(conn, runtime, session, proposal, receipt, delivery_id=delivery_id)
        return True
    except (OSError, sqlite3.Error):
        return False


def persist(conn, runtime, session, proposal, receipt, *, delivery_id=None):
    """Also used inside the admission/consumption owner's existing transaction."""
    rev = proposal.expected
    old = conn.execute("""SELECT status,reason,revision_json,target_id,action,incident_id,
        plugin_generation,owner,evidence_json,deadline,session_id FROM supervision_receipts
        WHERE profile=? AND proposal_id=?""", (rev.profile, proposal.proposal_id)).fetchone()
    revision = json.dumps(project(rev))
    evidence = json.dumps(list(proposal.evidence_refs))
    identity = (revision, proposal.target_id, proposal.action.value, proposal.incident_id,
                proposal.plugin_generation, proposal.owner, evidence, proposal.expires_at_monotonic, session)
    if old and tuple(old[2:]) != identity:
        raise sqlite3.IntegrityError("receipt_identity_changed")
    # The evidence-owner protocol explicitly returns to accepted/owner_selected
    # after dequeue, until exact postvalidation acknowledges consumption or vetoes.
    owner_selected = (receipt.status == "accepted" and receipt.reason == "owner_selected"
                      and old and old[0] == "selected")
    consumed = (receipt.status == "applied" and
                re.fullmatch(r"owner_consumed:[0-9a-f]{64}", receipt.reason) is not None)
    status = "consumed" if consumed else receipt.status
    if old and ((status == "accepted" and not owner_selected)
                or (old[0] in TERMINAL and status != old[0])):
        raise sqlite3.IntegrityError("receipt_already_settled")
    if not old and conn.execute("SELECT count(*) FROM supervision_receipts WHERE profile=?", (rev.profile,)).fetchone()[0] >= MAX_RECEIPTS:
        raise sqlite3.OperationalError("receipt_capacity")
    reason = receipt.reason if consumed or receipt.reason in REASONS else "owner_outcome"
    now = time.time()
    conn.execute("""INSERT INTO supervision_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(profile,proposal_id) DO UPDATE SET status=excluded.status,
        reason=excluded.reason,watermark=excluded.watermark,receipt_id=excluded.receipt_id,
        delivery_id=coalesce(excluded.delivery_id,supervision_receipts.delivery_id),updated_at=excluded.updated_at""",
        (rev.profile, proposal.proposal_id, rev.lineage, rev.work_id, session,
         proposal.incident_id, proposal.plugin_generation, proposal.owner, proposal.target_id,
         proposal.action.value, revision, evidence,
         proposal.expires_at_monotonic, status, reason, receipt.watermark,
         receipt.receipt_id, delivery_id, now, now))


def lookup(runtime, proposal_id):
    try:
        with writer(runtime) as (conn, _):
            return conn.execute("SELECT status,reason,watermark,receipt_id FROM supervision_receipts WHERE profile=? AND proposal_id=?",
                                (runtime.revision.profile, proposal_id)).fetchone()
    except (OSError, sqlite3.Error):
        return None
