"""Authenticated same-work steering triage and native consumer early findings."""
import json
import os
from pathlib import Path
import sys

import pytest

# Same explicit disposable-HOME pointer as the native views cross-repo suite.
source_pointer = Path.home() / '.hermes' / 'jev-supervisor-test-source'
source_root = os.environ.get('JEV_SUPERVISOR_SOURCE')
if not source_root and source_pointer.is_file():
    source_root = source_pointer.read_text().strip()
if not source_root:
    pytest.skip('JEV_SUPERVISOR_SOURCE required for standalone plugin contract', allow_module_level=True)
sys.path.insert(0, str(Path(source_root) / 'src'))

from agent import supervision_store as store
from agent.supervision_context import steer_from_user
from tests.agent.supervision_test_support import rig, accept
from tests.agent.test_supervision_delivery_native import native, final_launch, consume, rows, controls
from tools import async_delegation as source
from tools.process_registry import process_registry


def test_same_owned_work_late_result_after_steer_can_park(native):
    rt, launch = final_launch(native)
    old = rt.revision
    steer_from_user(native.rig.agent, '- Check the current `target.py` instead.')
    assert rt.revision.instruction_event != old.instruction_event
    assert rt.revision.work_id == old.work_id
    native.cli._drain_process_notifications('fixture-cli')
    assert native.calls and native.cli._pending_input.empty()
    assert source.get_durable_delegation(launch)['delivery_state'] == 'retained'
    facts = native.calls[-1]['state']['facts']['result']
    assert facts['input_revision_changed'] is True
    assert facts['original_revision']['instruction_event'] == old.instruction_event
    assert facts['current_revision']['instruction_event'] == rt.revision.instruction_event
    receipt = rows(native)[0]
    with source._transaction() as conn:
        delivery = store.get_admission(conn, receipt['delivery_id'])
        assert b'Exact provisional finding' in store.read_retained(conn, delivery['delivery_id'], session_id=native.cli.session_id)


@pytest.mark.parametrize('boundary', ['work_id', 'lineage', 'profile', 'generation', 'generation_unchanged', 'missing_generation', 'missing_controls'])
def test_changed_instruction_does_not_adopt_foreign_or_legacy_launch(native, boundary):
    rt, launch = final_launch(native)
    if boundary != 'generation_unchanged':
        steer_from_user(native.rig.agent, '- New current target.')
    with source._transaction() as conn:
        task = json.loads(conn.execute('SELECT task_json FROM async_delegations WHERE delegation_id=?', (launch,)).fetchone()[0])
        if boundary in {'work_id', 'lineage', 'profile'}:
            task['supervision_revision'][boundary] = 'foreign'
        elif boundary in {'generation', 'generation_unchanged'}:
            task['supervision_parent_generation'] += 1
        elif boundary == 'missing_generation':
            task.pop('supervision_parent_generation')
        else:
            conn.execute('UPDATE async_delegations SET control_child_ids=? WHERE delegation_id=?', ('[]', launch))
        conn.execute('UPDATE async_delegations SET task_json=? WHERE delegation_id=?', (json.dumps(task), launch))
    native.cli._drain_process_notifications('fixture-cli')
    assert not native.calls
    assert not native.cli._pending_input.empty()
    consume(native)
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'


def test_late_material_new_finding_is_delivered_after_steer(native):
    rt, launch = final_launch(native)
    steer_from_user(native.rig.agent, '- Check changed target.')
    native.mode['relation'] = 'corrects_material_error'
    native.cli._drain_process_notifications('fixture-cli')
    result, item = consume(native)
    assert 'Exact provisional finding' in str(item)
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'
    assert rows(native)[0]['status'] == 'consumed'


def test_launch_consumers_publish_early_finding_without_work_map(native):
    rt = accept(native.rig.agent, '- Check `target.py`.')
    assert not rt.work_maps
    children = controls(native, ['first', 'second'])
    with source._transaction() as conn:
        for child in ('first', 'second'):
            snapshot = json.loads(conn.execute('SELECT snapshot_json FROM delegation_controls WHERE child_id=?', (child,)).fetchone()[0])
            snapshot['obligation'] = 'required'
            snapshot['consumers'][0].update(requires_result=True, obligation='required', requirement_ids=[rt.requirements[0].id])
            conn.execute('UPDATE delegation_controls SET snapshot_json=? WHERE child_id=?', (json.dumps(snapshot), child))
    handle = source.dispatch_async_delegation_batch(goals=['Inspect `target.py`', 'Inspect `target.py`'], context=None,
        toolsets=[], role='default', model=None, session_key=native.cli.session_id, parent_session_id=native.cli.session_id,
        control_children=children, runner=lambda: {'results':[{'task_index':0, 'status':'completed', 'summary':'Final source'}]})
    launch = handle['delegation_id']
    entry = dict(task_index=0, status='completed', summary='Exact early native finding')
    source.record_unit_child(launch, entry)
    with native.bridge._lock:
        pending = tuple(native.bridge._pending)
    for future in pending:
        future.result(timeout=2)
    assert native.calls, json.dumps(native.bridge.supervisor.inspect())
    decisions = native.calls[-1]['state']['facts']['decisions']
    assert decisions[0]['source'] == 'native_launch_consumer'
    assert decisions[0]['text'] == rt.requirements[0].text
    rt.drain_at_safe_point()
    assert process_registry.completion_queue.qsize() == 1
    source.record_unit_child(launch, entry)
    rt.drain_at_safe_point()
    assert process_registry.completion_queue.qsize() == 1
    native.cli._drain_process_notifications('fixture-cli')
    _, item = consume(native)
    assert 'Exact early native finding' in str(item) and 'final batch remains outstanding' in str(item)
    assert source.get_durable_delegation(launch)['delivery_state'] == 'pending'
    native.executor.finish()
    native.cli._drain_process_notifications('fixture-cli')
    consume(native)
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'


def test_source_read_cannot_rebind_late_result_to_a_new_scope(native, monkeypatch):
    rt, launch = final_launch(native)
    original = store.get_result_object
    changed = []
    def read(conn, object_id):
        payload = original(conn, object_id)
        if not changed:
            changed.append(True)
            accept(native.rig.agent, '- An unrelated new task.')
        return payload
    monkeypatch.setattr(store, 'get_result_object', read)
    native.cli._drain_process_notifications('fixture-cli')
    assert changed and not native.calls
    assert not native.cli._pending_input.empty()
    consume(native)
    assert source.get_durable_delegation(launch)['delivery_state'] == 'delivered'
