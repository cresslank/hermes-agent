"""Canonical bounded owner records, using the existing SessionDB transaction owner.

Records hold source identities and decisions, never source bodies or semantic scores.
No external operation is replayed by recovery. Unknown/inflight rows are not evicted.
"""
import json
import copy
import sqlite3
import time

from agent.supervision_receipts import writer, _work, RETENTION_SECONDS

MAX_RECORDS = 64
MAX_PROFILE_RECORDS = 4096
MAX_RECORD_BYTES = 16384
KINDS = frozenset({"planning", "research_pass", "gap_obligation", "claim_delivery", "correction"})
TERMINAL = frozenset({"superseded", "withdrawn_by_parent", "disposition_committed", "stale", "issued", "invalid"})


def save(runtime, rows, *, validate=None, predecessors=None, predecessor_records=None):
    """Atomically replace the named graph records. Fail closed on storage/capacity."""
    runtime._assert_owner(tool_worker=True)
    try:
        encoded = []
        for row in rows:
            if (type(row) is not dict or type(row.get("id")) is not str or not 0 < len(row["id"]) <= 256
                    or type(row.get("revision")) is not int or row["revision"] < 1
                    or type(row.get("status")) is not str or not 0 < len(row["status"]) <= 64):
                return False
            stored = copy.deepcopy(row)
            d = stored.get("declaration", {})
            if d.get("type") == "delegation_candidate":
                from agent.supervision_efficiency import _pin
                op = d["operation"]
                stored["operation_pin"] = _pin((op["tool_name"], op["arguments"]))
                task = op["arguments"].get("tasks")
                if type(task) is not list or len(task) != 1 or set(task[0]) != {"goal", "context", "supervision"}:
                    return False
                # The immutable source owners retain these bytes, not this table.
                op["arguments"] = {"tasks": [{"supervision": task[0]["supervision"]}]}
            body = json.dumps(stored, sort_keys=True, separators=(",", ":"), allow_nan=False)
            if row["kind"] not in KINDS or len(body.encode()) > MAX_RECORD_BYTES:
                return False
            encoded.append((row, body))
        rev = runtime.revision
        key = (rev.profile, rev.lineage, rev.work_id)
        with writer(runtime) as (conn, session):
            if validate is not None and not validate():
                return False
            _work(conn, runtime, session)
            existing = {r[0] for r in conn.execute("SELECT record_id FROM supervision_owner_records WHERE profile=? AND lineage=? AND work_id=?", key)}
            novel = {r["id"] for r, _ in encoded} - existing
            count = conn.execute("SELECT count(*) FROM supervision_owner_records WHERE profile=?", (rev.profile,)).fetchone()[0]
            if len(existing | novel) > MAX_RECORDS or count + len(novel) > MAX_PROFILE_RECORDS:
                return False
            for row, body in encoded:
                old = conn.execute("SELECT kind,session_id,revision,status,body_json FROM supervision_owner_records WHERE profile=? AND lineage=? AND work_id=? AND record_id=?",
                                   (*key, row["id"])).fetchone()
                if predecessors is not None and (old is None or old[2:4] != predecessors.get(row["id"])):
                    raise sqlite3.IntegrityError("owner_record_predecessor_changed")
                if predecessor_records is not None:
                    expected = json.dumps(predecessor_records.get(row["id"]), sort_keys=True,
                        separators=(",", ":"), allow_nan=False)
                    if old is None or old[4] != expected:
                        raise sqlite3.IntegrityError("owner_record_body_changed")
                if old:
                    if old == (row["kind"], session, row["revision"], row["status"], body):
                        continue  # exact acknowledged write replay only
                    if old[:2] != (row["kind"], session) or old[2] != row["revision"] - 1:
                        raise sqlite3.IntegrityError("owner_record_revision_conflict")
                elif row["revision"] != 1:
                    raise sqlite3.IntegrityError("owner_record_missing_predecessor")
                conn.execute("""INSERT INTO supervision_owner_records
                    (profile,lineage,work_id,record_id,kind,session_id,revision,status,body_json,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(profile,lineage,work_id,record_id)
                    DO UPDATE SET revision=excluded.revision,status=excluded.status,
                    body_json=excluded.body_json,updated_at=excluded.updated_at""",
                    (*key, row["id"], row["kind"], session, row["revision"], row["status"], body, time.time()))
        return True
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return False


def load(runtime):
    """Rehydrate exact records only for the caller's current canonical work identity."""
    try:
        with writer(runtime) as (conn, _):
            rev = runtime.revision
            rows = conn.execute("SELECT body_json,status FROM supervision_owner_records WHERE profile=? AND lineage=? AND work_id=? ORDER BY rowid LIMIT ?",
                                (rev.profile, rev.lineage, rev.work_id, MAX_RECORDS + 1)).fetchall()
            if len(rows) > MAX_RECORDS:
                return ()
            records = []
            for body, status in rows:
                if len(body.encode()) > MAX_RECORD_BYTES:
                    return ()
                row = json.loads(body)
                row["status"] = status  # reset/retention fences override historical JSON
                records.append(row)
            return tuple(records)
    except (OSError, sqlite3.Error, ValueError, TypeError):
        return ()


def current(runtime, row):
    """Deletion, reset, retention and another native revision fence cached graphs."""
    try:
        with writer(runtime) as (conn, _):
            r = runtime.revision
            stored = conn.execute("SELECT revision,status FROM supervision_owner_records WHERE profile=? AND lineage=? AND work_id=? AND record_id=?",
                                  (r.profile, r.lineage, r.work_id, row["id"])).fetchone()
            return stored == (row["revision"], row["status"])
    except (OSError, sqlite3.Error):
        return False


def prune(conn, now):
    marks = ",".join("?" for _ in TERMINAL)
    conn.execute(f"DELETE FROM supervision_owner_records WHERE updated_at<? AND status IN ({marks})",
                 (now - RETENTION_SECONDS, *sorted(TERMINAL)))
