"""Recover native lifecycle facts without replaying semantic actions or tools.

Caller holds the existing owner lock. Each attempt is one zero-wait canonical
CAS per ancestor, never a polling loop or a new deadline. Only rolled-back SQLite
contention is retryable; generation loss, CAS conflicts and I/O ambiguity retain
unknown durable work. Nothing here reconstructs completions after process loss.
"""
import logging
import sqlite3


logger = logging.getLogger(__name__)


def recover_lifecycle(owner, live):
    from agent.owned_delegation import ControlDenied
    from tools.delegation_control_store import ControlConflict

    if live.finalization_failed:
        return False
    finish = live.finish_pending and not live.snapshot['worker_finished']
    exits = live.completed_dispatches
    handoffs = live.completed_handoffs.intersection(live.snapshot['handoffs'])
    if exits or handoffs or finish:
        def change(s):
            for dispatch_id in exits:
                if dispatch_id not in s['dispatches']:
                    raise ControlDenied('Missing owned dispatch completion identity')
                nested = s['dispatches'].pop(dispatch_id)
                s['inflight'] -= 1
                if nested:
                    s['handoffs'].remove('dispatch:nested')
            for handoff in handoffs:
                s['handoffs'].remove(handoff)
            s['worker_finished'] = s['worker_finished'] or finish
            owner._settle(s)

        try:
            owner._commit(live, change, finalizer=True)
        except (sqlite3.Error, ControlConflict, ControlDenied, OSError) as exc:
            code = getattr(exc, 'sqlite_errorcode', 0) & 0xff
            if isinstance(exc, sqlite3.OperationalError) and code in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                return False  # transaction rolled back; retain exact completed facts
            live.finalization_failed = True
            logger.warning('Owned lifecycle accounting is unknown; automatic reconciliation fenced')
            return False
        live.completed_dispatches.clear()
        live.completed_handoffs.clear()
        live.finish_pending = False
    elif live.snapshot['worker_finished']:
        live.finish_pending = False

    if live.snapshot['settled']:
        live.child = None
        if live.parent_child_id:
            ancestor = owner._live[live.parent_child_id]
            if live.handle.child_id in ancestor.snapshot['handoffs']:
                ancestor.completed_handoffs.add(live.handle.child_id)
                return recover_lifecycle(owner, ancestor)
    return True
