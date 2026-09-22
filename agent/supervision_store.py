"""Provider-neutral durable admission primitives, on the existing state.db writer.

Functions taking ``conn`` run INSIDE the caller's writer transaction. They never
commit, open another database, append a transcript or infer policy from prose.
The source owner and SessionDB reuse these functions to transfer ownership atomically.
"""
from __future__ import annotations

import hashlib
import json
import os
import time

OBJECT_LIMIT = 4 * 1024 * 1024
PROFILE_LIMIT = 64 * 1024 * 1024
RETENTION_SECONDS = 7 * 24 * 3600

# Alias c is intentional: reuse this exact predicate in each retention owner.
# Missing/corrupt settlement is unknown, never permission to delete a parent.
UNSETTLED_CONTROL_SQL = """CASE WHEN json_valid(c.snapshot_json) THEN
    json_extract(c.snapshot_json, '$.settled') IS NOT 1
    OR json_extract(c.snapshot_json, '$.processes_stopped') IS NOT 1
    OR json_extract(c.snapshot_json, '$.effects_reconciled') IS NOT 1
    OR json_extract(c.snapshot_json, '$.cleanup_pending') IS NOT 0
    OR json_extract(c.snapshot_json, '$.inflight') IS NOT 0
    OR json_extract(c.snapshot_json, '$.handoffs') IS NOT '[]'
    ELSE 1 END"""


def protected_control_sessions(conn):
    return {r[0] for r in conn.execute(
        f'SELECT c.parent_session_id FROM delegation_controls c WHERE {UNSETTLED_CONTROL_SQL}')}



class AdmissionError(RuntimeError):
    """A fenced or unavailable admission; the source must not be acknowledged."""


class RetentionCapacity(AdmissionError):
    """Optional retention cannot secure bytes. Use ordinary delivery, not suppression."""


def _dict(cursor):
    row = cursor.fetchone()
    return dict(zip((c[0] for c in cursor.description), row)) if row is not None else None


def profile_key() -> str:
    from hermes_constants import get_hermes_home
    return hashlib.sha256(os.fsencode(str(get_hermes_home().resolve()))).hexdigest()


def digest(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=True, separators=(',', ':')).encode()).hexdigest()


def generation(conn, session_id):
    row = conn.execute('SELECT generation, revoked FROM supervision_generations WHERE session_id=?', (session_id,)).fetchone()
    if row is not None and row[1]:
        raise AdmissionError('target generation revoked')
    if conn.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone() is None:
        raise AdmissionError('target session missing')
    return row[0] if row else 1


def lineage(conn, session_id):
    """Compression continues one delivery identity; reset never does."""
    seen = set()
    while session_id not in seen:
        seen.add(session_id)
        row = conn.execute('SELECT s.parent_session_id FROM sessions s JOIN sessions p ON p.id=s.parent_session_id '
                           "WHERE s.id=? AND p.end_reason='compression'", (session_id,)).fetchone()
        if not row:
            return session_id
        session_id = row[0]
    raise AdmissionError('cyclic session lineage')


def put_result_object(conn, *, launch_id, source_id, subtype, payload: bytes, session_id, profile=None):
    if not isinstance(payload, bytes):
        raise TypeError('immutable source must be exact bytes')
    profile = profile or profile_key()
    tombstone = conn.execute('SELECT revoked FROM supervision_generations WHERE session_id=?', (session_id,)).fetchone()
    if tombstone and tombstone[0] and conn.execute('SELECT 1 FROM sessions WHERE id=?', (session_id,)).fetchone() is None:
        raise AdmissionError('source session deleted')
    # A reset revokes delivery, not an already-owned worker's ability to commit
    # its final evidence under the original work. Deletion removes that launch.
    content_hash = hashlib.sha256(payload).hexdigest()
    object_id = digest(profile, launch_id, source_id, content_hash)
    conn.execute('INSERT OR IGNORE INTO delegation_result_objects '
                 '(object_id,profile,session_id,launch_id,source_id,subtype,content_hash,payload,created_at) '
                 'VALUES(?,?,?,?,?,?,?,?,?)',
                 (object_id, profile, session_id, launch_id, source_id, subtype, content_hash, payload, time.time()))
    return object_id


def get_admission(conn, delivery_id):
    return _dict(conn.execute('SELECT * FROM supervision_admissions WHERE delivery_id=?', (delivery_id,)))


def get_result_object(conn, object_id) -> bytes:
    row = conn.execute('SELECT payload,content_hash FROM delegation_result_objects WHERE object_id=?', (object_id,)).fetchone()
    if row is None:
        raise AdmissionError('source object missing')
    payload = bytes(row[0])
    if hashlib.sha256(payload).hexdigest() != row[1]:
        raise AdmissionError('source object integrity failure')
    return payload


def pin_result_effect(conn, object_id, receipt_ref):
    """Attach an unresolved effect/cleanup receipt under its source owner's transaction."""
    if not isinstance(receipt_ref, str) or not receipt_ref or len(receipt_ref) > 512:
        raise ValueError('invalid effect receipt reference')
    row = conn.execute('SELECT effect_refs_json FROM delegation_result_objects WHERE object_id=?', (object_id,)).fetchone()
    if row is None:
        raise AdmissionError('effect source missing')
    refs = json.loads(row[0])
    if receipt_ref not in refs:
        if len(refs) >= 64:
            raise RetentionCapacity('effect reference capacity')
        refs.append(receipt_ref)
    conn.execute('UPDATE delegation_result_objects SET effect_pending=1,effect_refs_json=?,settled_at=NULL WHERE object_id=?',
                 (json.dumps(refs),object_id))


def settle_result_effect(conn, object_id, receipt_ref):
    row = conn.execute('SELECT effect_refs_json,launch_id FROM delegation_result_objects WHERE object_id=?', (object_id,)).fetchone()
    if row is None:
        raise AdmissionError('effect source missing')
    refs = json.loads(row[0])
    if receipt_ref not in refs:
        return False
    refs.remove(receipt_ref)
    conn.execute('UPDATE delegation_result_objects SET effect_pending=?,effect_refs_json=? WHERE object_id=?',
                 (int(bool(refs)),json.dumps(refs),object_id))
    if not refs:
        conn.execute("UPDATE delegation_result_objects SET settled_at=? WHERE object_id=? "
                     "AND launch_id IN (SELECT delegation_id FROM async_delegations WHERE delivery_state='delivered') "
                     "AND object_id NOT IN (SELECT object_id FROM supervision_admissions WHERE state!='consumed')",
                     (time.time(),object_id))
    return True


def protect_optional(conn, object_id):
    row = conn.execute('SELECT profile,length(payload) FROM delegation_result_objects WHERE object_id=?', (object_id,)).fetchone()
    if not row or row[1] > OBJECT_LIMIT:
        raise RetentionCapacity('object retention budget')
    # Count distinct objects, not inbox references. Unresolved source objects also
    # count: admitting a new optional pin must never evict uncertain evidence.
    used = conn.execute('SELECT coalesce(sum(length(payload)),0) FROM delegation_result_objects '
                        'WHERE profile=? AND (settled_at IS NULL OR effect_pending=1 OR object_id IN '
                        "(SELECT object_id FROM supervision_admissions WHERE state!='consumed'))", (row[0],)).fetchone()[0]
    if used > PROFILE_LIMIT:
        raise RetentionCapacity('profile retention budget')


def persist_admission(conn, *, event, claim, target_session_id, disposition='deliver_unchanged', retainable=False,
                      view_window=None, defer_until=None):
    """Persist source claim + pin, without implying destination availability.

    ``retainable`` is a HOST owner validation, never a model-returned flag. All
    unknown/invalid dispositions preserve normal delivery. Findings use their own
    immutable source identity and never mutate the final source delivery row.
    """
    profile = profile_key()
    target_generation = generation(conn, target_session_id)
    root = lineage(conn, target_session_id)
    launch = str(event['delegation_id'])
    finding = event.get('finding_id')
    subtype = 'finding' if finding else 'final'
    source = str(finding or 'final')
    delivery = digest(profile, root, launch, source, subtype)
    prior = get_admission(conn, delivery)
    if prior:
        saved = json.loads(prior['event_json'])
        for key in ('summary','results','status','error','source_receipt','verified'):
            if key in event and event[key] != saved.get(key):
                raise AdmissionError('immutable delivery payload changed')
        if prior['state'] == 'persisted':
            row = conn.execute('SELECT delivery_claim FROM async_delegations WHERE delegation_id=?', (launch,)).fetchone()
            if subtype == 'final' and (not row or row[0] != claim):
                raise AdmissionError('source claim changed')
            conn.execute('UPDATE supervision_admissions SET source_claim=? WHERE delivery_id=?', (claim, delivery))
        return get_admission(conn, delivery)
    row = _dict(conn.execute('SELECT * FROM async_delegations WHERE delegation_id=?', (launch,)))
    if row is None:
        raise AdmissionError('source launch missing')
    if subtype == 'final' and (not row['event_json'] or row['state'] in {'running','finalizing','stalling'}):
        raise AdmissionError('final source not committed')
    if subtype == 'final' and row['event_json']:
        saved = json.loads(row['event_json'])
        for key in ('summary','results','status','error'):
            if key in event and event[key] != saved.get(key):
                raise AdmissionError('source event bytes changed')
    if subtype == 'final' and (row['delivery_state'] != 'pending' or row['delivery_claim'] != claim):
        raise AdmissionError('source claim changed')
    object_id = event.get('source_object_id')
    if object_id:
        obj = conn.execute('SELECT launch_id,source_id,subtype FROM delegation_result_objects WHERE object_id=?', (object_id,)).fetchone()
        if not obj or tuple(obj) != (launch, source, subtype):
            raise AdmissionError('source identity mismatch')
        get_result_object(conn, object_id)
    elif subtype == 'final':
        payload = (row['result_json'] or '{}').encode('utf-8')
        object_id = put_result_object(conn, launch_id=launch, source_id=source, subtype=subtype,
                                      payload=payload, session_id=row['parent_session_id'] or target_session_id)
    else:
        raise AdmissionError('finding requires committed source object')
    # An unknown/required obligation is never semantically suppressible.
    effect_pending = conn.execute('SELECT effect_pending FROM delegation_result_objects WHERE object_id=?', (object_id,)).fetchone()[0]
    retainable = (retainable and get_launch_control(conn, launch)['retainable'] and not effect_pending
                  and not event.get('error') and row['state'] in {'completed', 'success'})
    view_text, not_before = None, None
    if disposition == 'deliver_bounded_view' and retainable and view_window is not None:
        try:
            start, end = view_window
            payload = get_result_object(conn, object_id)
            if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(payload):
                raise ValueError('invalid exact window')
            if end - start > 24 * 1024 or event.get('error') or row['state'] not in {'completed','success'}:
                raise ValueError('mandatory failure/status cannot be clipped')
            view_text = payload[start:end].decode('utf-8')
        except (ValueError, TypeError, UnicodeDecodeError):
            disposition = 'deliver_unchanged'
    elif disposition == 'defer' and defer_until is not None:
        remaining = defer_until - time.monotonic()
        if 0 < remaining <= 0.150:
            not_before = time.time() + remaining
        else:
            disposition = 'deliver_unchanged'
    elif disposition != 'retain_without_wake' or not retainable:
        disposition = 'deliver_unchanged'
    if disposition == 'retain_without_wake':
        try:
            protect_optional(conn, object_id)
        except RetentionCapacity:
            disposition = 'deliver_unchanged'
    state = 'parked' if disposition == 'retain_without_wake' else 'persisted'
    now = time.time()
    conn.execute('INSERT INTO supervision_admissions '
                 '(delivery_id,profile,lineage,target_session_id,target_generation,launch_id,source_id,subtype,'
                 'source_claim,object_id,event_json,state,disposition,view_text,not_before,created_at,updated_at) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                 (delivery,profile,root,target_session_id,target_generation,launch,source,subtype,claim,object_id,
                  json.dumps(event, ensure_ascii=True),state,disposition,view_text,not_before,now,now))
    if state == 'parked' and subtype == 'final':
        conn.execute("UPDATE async_delegations SET delivery_state='retained' WHERE delegation_id=? AND delivery_claim=?", (launch,claim))
    return get_admission(conn, delivery)


def _lease_alive(lease):
    try:
        pid = int(lease.split(':', 1)[0])
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (ValueError, PermissionError, AttributeError):
        return True


def reserve(conn, delivery_id, *, destination_lease, capacity=256, lease_seconds=300):
    row = get_admission(conn, delivery_id)
    if not row:
        raise AdmissionError('unknown delivery')
    if generation(conn, row['target_session_id']) != row['target_generation']:
        raise AdmissionError('target generation changed')
    if row['state'] in {'parked','consumed'}:
        return False
    now = time.time()
    if row['disposition'] == 'defer':
        if (row['not_before'] or 0) > now:
            return False
        conn.execute("UPDATE supervision_admissions SET disposition='deliver_unchanged',not_before=NULL WHERE delivery_id=?", (delivery_id,))
    if row['state'] == 'accepted' and row['destination_lease'] != destination_lease:
        if (row['lease_expires'] or 0) > now and _lease_alive(row['destination_lease']):
            return False
    if row['state'] == 'persisted':
        count = conn.execute("SELECT count(*) FROM supervision_admissions WHERE target_session_id=? AND state='accepted'",
                             (row['target_session_id'],)).fetchone()[0]
        if count >= capacity:
            return False
        if row['subtype'] == 'final':
            changed = conn.execute("UPDATE async_delegations SET delivery_state='transferred' "
                                   "WHERE delegation_id=? AND delivery_state='pending' AND delivery_claim=?",
                                   (row['launch_id'], row['source_claim'])).rowcount
            if changed != 1:
                raise AdmissionError('source claim lost before transfer')
    conn.execute("UPDATE supervision_admissions SET state='accepted',destination_lease=?,lease_expires=?,updated_at=? WHERE delivery_id=?",
                 (destination_lease, now+lease_seconds,now,delivery_id))
    return True


def validate_consumption(conn, delivery_id, session_id, destination_lease):
    row = get_admission(conn, delivery_id)
    if not row or row['lineage'] != lineage(conn, session_id):
        raise AdmissionError('wrong destination lineage')
    generation(conn, session_id)
    if row['state'] == 'consumed':
        return row
    if row['state'] != 'accepted' or row['destination_lease'] != destination_lease:
        raise AdmissionError('destination lease lost')
    if row['lease_expires'] <= time.time():
        raise AdmissionError('destination lease expired')
    return row


def consume(conn, row, message_row_id):
    now = time.time()
    conn.execute("UPDATE supervision_receipts SET status='consumed',reason='admission_consumed',updated_at=? "
                 "WHERE delivery_id=? AND status='selected'", (now, row['delivery_id']))
    conn.execute("UPDATE supervision_admissions SET state='consumed',message_row_id=?,updated_at=? WHERE delivery_id=?",
                 (message_row_id,now,row['delivery_id']))
    if row['subtype'] == 'final':
        conn.execute("UPDATE async_delegations SET delivery_state='delivered',delivered_at=?,updated_at=? "
                     "WHERE delegation_id=? AND delivery_claim=? AND delivery_state='transferred'",
                     (now,now,row['launch_id'],row['source_claim']))
    conn.execute('UPDATE delegation_result_objects SET settled_at=? WHERE object_id=? AND effect_pending=0', (now,row['object_id']))
    if row['subtype'] == 'final':
        conn.execute("UPDATE delegation_result_objects SET settled_at=? WHERE launch_id=? AND subtype!='finding' "
                     "AND effect_pending=0 AND object_id NOT IN (SELECT object_id FROM supervision_admissions WHERE state!='consumed')",
                     (now, row['launch_id']))


def prune_objects(conn, now=None):
    from agent.supervision_receipts import prune
    prune(conn, now)
    cutoff = (time.time() if now is None else now) - RETENTION_SECONDS
    conn.execute(f"""DELETE FROM supervision_admissions WHERE state='consumed' AND updated_at<?
        AND NOT EXISTS (SELECT 1 FROM delegation_controls c
            WHERE c.parent_session_id=supervision_admissions.target_session_id
            AND (({UNSETTLED_CONTROL_SQL}) OR c.updated_at>=?))""", (cutoff, cutoff))
    conn.execute(f"""DELETE FROM delegation_result_objects WHERE settled_at<? AND effect_pending=0
        AND object_id NOT IN (SELECT object_id FROM supervision_admissions)
        AND NOT EXISTS (SELECT 1 FROM supervision_admissions a
            WHERE json_extract(a.event_json, '$.source_receipt')=delegation_result_objects.object_id)
        AND NOT EXISTS (SELECT 1 FROM delegation_controls c
            WHERE c.parent_session_id=delegation_result_objects.session_id
            AND (({UNSETTLED_CONTROL_SQL}) OR c.updated_at>=?))""", (cutoff, cutoff))


def read_retained(conn, delivery_id, *, session_id):
    """Explicit owner retrieval remains scoped to the original lineage, including parked work."""
    row = get_admission(conn, delivery_id)
    if not row or row['profile'] != profile_key() or row['lineage'] != lineage(conn, session_id):
        raise AdmissionError('retained source outside owner lineage')
    return get_result_object(conn, row['object_id'])


def promote_parked(conn, delivery_id, *, expected_generation):
    """Only an authorized owner material-change action calls this, never ordinary recovery."""
    row = get_admission(conn, delivery_id)
    if not row or row['state'] != 'parked':
        raise AdmissionError('not a parked delivery')
    if expected_generation != row['target_generation'] or generation(conn, row['target_session_id']) != expected_generation:
        raise AdmissionError('promotion generation changed')
    if row['subtype'] == 'final':
        changed = conn.execute("UPDATE async_delegations SET delivery_state='pending' WHERE delegation_id=? "
                               "AND delivery_state='retained' AND delivery_claim=?",
                               (row['launch_id'], row['source_claim'])).rowcount
        if changed != 1:
            raise AdmissionError('retained claim lost')
    conn.execute("UPDATE supervision_admissions SET state='persisted',disposition='deliver_unchanged',updated_at=? WHERE delivery_id=?",
                 (time.time(), delivery_id))
    return get_admission(conn, delivery_id)


def get_launch_control(conn, launch_id):
    """Read canonical child controls in the admission transaction; never copy authority.

    Each unit must link every child, including unknown legacy IDs. Partial, corrupt
    or foreign linkage cannot turn a mixed required/optional batch into optional work.
    The host decision is still required; these durable checks can only restrict it.
    """
    row = _dict(conn.execute('SELECT parent_session_id,control_child_ids,task_json '
                             'FROM async_delegations WHERE delegation_id=?', (launch_id,)))
    if row is None:
        raise AdmissionError('launch control missing')
    unknown = {'obligation': 'unknown', 'children': [], 'retainable': False}
    try:
        ids = json.loads(row['control_child_ids'])
        task = json.loads(row['task_json'] or '{}')
        expected = len(task.get('task_indexes') if task.get('task_indexes') is not None
                       else task.get('goals', [task.get('goal')]))
        if (not isinstance(ids, list) or len(ids) != expected or not ids
                or any(not isinstance(i, str) or not i for i in ids) or len(set(ids)) != len(ids)):
            return unknown
        children = []
        for child_id in ids:
            control = _dict(conn.execute('SELECT * FROM delegation_controls WHERE child_id=?', (child_id,)))
            if not control or control['parent_session_id'] != row['parent_session_id']:
                return unknown
            snapshot = json.loads(control['snapshot_json'])
            if any(snapshot.get(k) != control[k] for k in ('child_id','parent_session_id','generation','control_revision')):
                return unknown
            children.append(snapshot)
        def optional(s):
            consumers = s.get('consumers')
            return (s.get('obligation') == 'optional' and s.get('consumer_set_closed') is True
                    and s.get('effect_class') == 'read_only' and bool(s.get('effect_policy_id'))
                    and isinstance(consumers, list) and bool(consumers)
                    and all(isinstance(c, dict) and c.get('ref') and c.get('obligation') == 'optional'
                            and all(c.get(k) is False for k in ('requires_result','requires_effects','requires_cleanup'))
                            for c in consumers)
                    and any(c.get('ref') == 'parent:' + row['parent_session_id'] for c in consumers)
                    and all(s.get(k) is True for k in ('settled','processes_stopped','effects_reconciled'))
                    and s.get('cleanup_pending') is False and s.get('handoffs') == [] and s.get('inflight') == 0)
        retainable = all(optional(s) for s in children)
        obligation = 'optional' if retainable else ('required' if any(s.get('obligation') == 'required' for s in children) else 'unknown')
        return {'obligation': obligation, 'children': children, 'retainable': retainable}
    except (ValueError, TypeError, AttributeError):
        return unknown
