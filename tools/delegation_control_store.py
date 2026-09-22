"""Canonical state.db adapter for owned delegation control.

DDL belongs to hermes_state_common.SCHEMA_SQL (delivery owner), not this module.
Every transition is a durable compare-and-swap; missing schema/storage fails closed.
No connection, agent, or bearer capability is exposed to a supervisor plugin.
"""
from __future__ import annotations

import json
import time
from typing import Callable


class ControlConflict(ValueError):
    """The durable child revision changed or the identity already exists."""


class ControlDeadlineExpired(ValueError):
    """The propagated action deadline expired before durable commit."""


class SQLiteControlStore:
    def __init__(self, connect: Callable | None = None, *, authorize: Callable | None = None,
                 wait_for_writer: bool = True):
        if connect is None:
            from tools.async_delegation import _connect
            connect = _connect
        self._connect = connect
        self._authorize = authorize
        self._wait_for_writer = wait_for_writer

    def _budget(self, conn, deadline):
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ControlDeadlineExpired('Delegation action deadline expired')
            # SQLite's busy timeout counts requested sleeps, not elapsed wall
            # time. A synchronous owner holding outer fences cannot wait here:
            # contention must abstain, not monopolize instruction/revoke locks.
            millis = max(0, min(10000, int(remaining * 1000))) if self._wait_for_writer else 0
            conn.execute(f"PRAGMA busy_timeout={millis}")

    def _authority(self, conn, deadline=None):
        if self._authorize is not None and not self._authorize(deadline=deadline, connection=conn):
            raise ControlConflict('Delegation authority unavailable')

    def create(self, snapshot: dict) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute('BEGIN IMMEDIATE')
                self._authority(conn)
                conn.execute(
                    "INSERT INTO delegation_controls "
                    "(child_id,parent_session_id,generation,control_revision,snapshot_json,updated_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (snapshot['child_id'], snapshot['parent_session_id'], snapshot['generation'],
                     snapshot['control_revision'], json.dumps(snapshot, sort_keys=True), time.time()),
                )
        finally:
            conn.close()

    def compare_and_swap(self, snapshot: dict, expected_revision: int, *, deadline: float | None = None) -> None:
        if snapshot['control_revision'] != expected_revision + 1:
            raise ControlConflict('Control revisions must advance exactly once')
        conn = self._connect()
        try:
            with conn:
                self._budget(conn, deadline)
                conn.execute('BEGIN IMMEDIATE')
                self._budget(conn, deadline)
                if deadline is not None:
                    # Recheck session membership after contention, in the very
                    # transaction that will publish the semantic control effect.
                    self._authority(conn, deadline)
                    self._budget(conn, deadline)
                result = conn.execute(
                    "UPDATE delegation_controls SET control_revision=?,snapshot_json=?,updated_at=? "
                    "WHERE child_id=? AND parent_session_id=? AND generation=? AND control_revision=?",
                    (snapshot['control_revision'], json.dumps(snapshot, sort_keys=True), time.time(),
                     snapshot['child_id'], snapshot['parent_session_id'], snapshot['generation'], expected_revision),
                )
                if result.rowcount != 1:
                    raise ControlConflict('Stale delegation control revision')
                if deadline is not None:
                    self._authority(conn, deadline)
                # Commit may itself wait in rollback-journal mode. It gets only
                # what remains, never the earlier BEGIN's full busy timeout.
                self._budget(conn, deadline)
        finally:
            conn.close()

    def read(self, child_id: str) -> dict | None:
        conn = self._connect()
        try:
            row = conn.execute('SELECT snapshot_json FROM delegation_controls WHERE child_id=?', (child_id,)).fetchone()
            return json.loads(row[0]) if row else None
        finally:
            conn.close()
