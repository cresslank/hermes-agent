"""Real standalone F12 through native catalog/detail and committed todo phase."""
import copy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from tests.agent.test_supervision_native_views import native as native, skill_catalog
from tests.agent.test_supervision_views import assemble
from tests.agent.test_tool_call_incremental_persistence import _mock_tool_call, _make_tool_defs


def todo(native, messages, items=None, concurrent=False):
    a = native.agent
    a.tools = _make_tool_defs('todo_list')
    args = {} if items is None else {'todos': items}
    call = _mock_tool_call(name='todo_list', call_id='todo-' + str(len(messages)))
    call.function.arguments = json.dumps(args)
    assistant = SimpleNamespace(content='', tool_calls=[call])
    messages.append({'role': 'assistant', 'content': '', 'tool_calls': [
        {'id': call.id, 'type': 'function', 'function': {'name': 'todo_list', 'arguments': call.function.arguments}}]})
    if concurrent:
        a._execute_tool_calls_concurrent(assistant, messages, 'task')
    else:
        a._execute_tool_calls_sequential(assistant, messages, 'task')
    result = next(m for m in reversed(messages) if m.get('role') == 'tool' and m.get('tool_call_id') == call.id)
    assert json.loads(result['content'])['todos'] == a._todo_store.read()


def ready(native):
    skill_catalog(native)
    messages: list[dict] = [{'role': 'user', 'content': 'Analyze weather observations.'},
                {'role': 'tool', 'name': 'skill_view', 'tool_call_id': 'loaded-before',
                 'content': 'Loaded skill body. Mandatory safety and cleanup remain in history.'}]
    items = [{'id': 'analysis', 'content': 'Analyze weather observations.', 'status': 'in_progress'},
             {'id': 'safety', 'content': 'Verify required safety checks.', 'status': 'pending'},
             {'id': 'cleanup', 'content': 'Clean up the owned scratch artifacts.', 'status': 'pending'}]
    todo(native, messages, items)
    result = assemble(native.agent, messages)
    body = (native.home / 'skills/alpha/SKILL.md').read_text()
    assert body in result.api_messages[-1]['content'], native.bridge.supervisor.inspect()
    native.mode['presented_skill_rows'] = copy.deepcopy(result.api_messages)
    binding = native.agent._supervision_view_binding
    entry, = binding.skill_hints.values()
    assert entry[0].plugin_id == 'fixture-views'
    assert entry[0].registration == entry[3].generation
    return messages, items, binding, entry


def removal_calls(native):
    return [b for b, _ in native.calls if 'F12/remaining' in b['questions']]


@pytest.mark.parametrize('concurrent', [False, True])
def test_ready_hint_exact_removal_at_committed_native_phase(native, monkeypatch, concurrent):
    native.mode['delay'] = .005
    messages, items, binding, entry = ready(native)
    hint = entry[0]
    skills = binding.views.skills
    skills.rank(('beta',), revision=skills.revision, plugin_id='other-plugin', ambiguous=True)
    foreign = skills.hints['other-plugin']
    originals = copy.deepcopy(messages)
    sources = copy.deepcopy(native.runtime.sources)
    requirements = native.runtime.requirements
    ranked = skills.ranked_ids
    proposals, observations = [], []
    observe = entry[3].consumer
    def observe_bound(snapshot):
        observations.append(copy.deepcopy(snapshot))
        return observe(snapshot)
    monkeypatch.setattr(entry[3], 'consumer', observe_bound)
    submit = native.facade.submit
    def capture(p):
        proposals.append(copy.deepcopy(p))
        return submit(p)
    monkeypatch.setattr(native.facade, 'submit', capture)
    todo(native, messages)  # ordinary evidence refresh before phase completion
    assemble(native.agent, messages)
    assert binding.skill_hints[hint.plugin_id] is entry
    assert binding.views.skills.owned_hints[hint.plugin_id] is hint
    assert binding.views.skills.hints['other-plugin'] == foreign
    assert [b['state']['facts']['stage'] for b, _ in native.calls] == ['metadata', 'detail']
    todo(native, messages, [{**t, 'status': 'completed'} for t in items], concurrent)
    native.drain()
    assert skills.hints == {'other-plugin': foreign}, native.bridge.supervisor.inspect()
    assert skills.ranked_ids == ranked
    assert messages[:len(originals)] == originals
    assert native.runtime.sources == sources and native.runtime.requirements == requirements
    assert len(removal_calls(native)) == 1
    body = removal_calls(native)[0]
    unload, = [o for o in observations if o['event'] == 'task_revision' and o['facts'].get('stage') == 'unload']
    assert unload['facts']['hint']['id'] == hint.id
    assert body['state']['facts']['stage'] == 'unload'
    assert body['state']['facts']['hint']['id'] == hint.id
    proposal, = [p for p in proposals if p['metadata']['feature_action'] == 'remove_own_hint']
    assert proposal['action'] == 'select_skills' and proposal['candidate_ids'] == []
    assert proposal['metadata']['hint_id'] == hint.id
    assert native.runtime.receipts[proposal['proposal_id']].status == 'applied'
    from agent.supervision_receipts import lookup
    persisted = lookup(native.runtime, proposal['proposal_id'])
    assert persisted is not None and persisted[:2] == ('applied', 'native_view')
    assert not binding.skill_hints and not binding.skill_removals
    native.mode['retirement_trace'] = {'observations': observations, 'proposals': proposals,
        'receipt': vars(native.runtime.receipts[proposal['proposal_id']]), 'hint': vars(hint),
        'surviving_hints': dict(skills.hints), 'history_unchanged': messages[:len(originals)] == originals}
    deadline = native.runtime.round_deadline
    for extra_batch in (False, True):
        if extra_batch:
            todo(native, messages)  # unrelated evidence refresh, not a new phase
        before_assembly = copy.deepcopy(messages)
        result = assemble(native.agent, messages)
        native.drain()
        presented = native.mode['presented_skill_rows']
        assert result.api_messages[:len(presented)] == presented  # cached prefix is immutable
        assert hint.content not in result.api_messages[-1]['content']
        assert foreign not in str(result.api_messages)  # unbound recommendations are not bodies
        assert binding.skill_contents() == ()
        assert binding.views.skills.hints == {'other-plugin': foreign}
        assert not binding.skill_hints
        assert messages == before_assembly
        assert [b['state']['facts']['stage'] for b, _ in native.calls] == ['metadata', 'detail', 'unload']
        if not extra_batch:
            assert native.runtime.round_deadline == deadline  # assembly cannot renew the completion window
    native.mode['retirement_trace']['next_request'] = result.api_messages
    native.mode['retirement_trace']['next_request_hints'] = dict(binding.views.skills.hints)
    native.mode['retirement_trace']['stages'] = [b['state']['facts']['stage'] for b, _ in native.calls]
    assert len(removal_calls(native)) == 1


@pytest.mark.parametrize('case', ['pending', 'safety', 'cleanup', 'cancelled', 'deleted', 'rewritten',
                                  'added', 'read', 'mandatory', 'must_keep', 'safety_hint',
                                  'catalog_required', 'catalog_revision', 'no_hint', 'no_grant', 'no_policy'])
def test_phase_producer_preserves_incomplete_required_and_absent_hints(native, case):
    messages, items, binding, entry = ready(native)
    hint, task, plan, reg = entry
    finished = [{**t, 'status': 'completed'} for t in items]
    if case in ('pending', 'safety', 'cleanup'):
        finished[{'pending': 0, 'safety': 1, 'cleanup': 2}[case]]['status'] = 'pending'
    elif case == 'cancelled':
        finished[0]['status'] = 'cancelled'
    elif case == 'deleted':
        finished.pop()
    elif case == 'rewritten':
        finished[0]['content'] = 'Different work'
    elif case == 'added':
        finished.append({'id': 'new', 'content': 'New work', 'status': 'completed'})
    elif case == 'read':
        finished = None
    elif case in ('mandatory', 'must_keep', 'safety_hint'):
        field = 'safety_or_cleanup' if case == 'safety_hint' else case
        protected = replace(hint, **{field: True})
        binding.skill_hints[hint.plugin_id] = (protected, task, plan, reg)
        binding.views.skills.owned_hints[hint.plugin_id] = protected
    elif case == 'catalog_required':
        binding.views.skills.candidates = tuple(replace(c, required=True) if c.id == hint.skill_id else c
                                               for c in binding.views.skills.candidates)
    elif case == 'catalog_revision':
        binding.views.skills.revision = 'changed'
    elif case == 'no_hint':
        binding.views.skills.hints.pop(hint.plugin_id)
    elif case == 'no_grant':
        reg.grants = frozenset({'observe'})
    elif case == 'no_policy':
        reg.egress_policy = {}
    before = dict(binding.views.skills.hints)
    todo(native, messages, finished)
    native.drain()
    assert not removal_calls(native)
    assert binding.views.skills.hints == before
    if case in {'mandatory', 'must_keep', 'safety_hint', 'catalog_required', 'no_grant', 'no_policy'}:
        calls = len(native.calls)
        assemble(native.agent, messages)
        assert binding.views.skills.hints == before
        assert len(native.calls) == calls


@pytest.mark.parametrize('case', ['remaining', 'hint_id', 'feature_action', 'feature_id', 'candidate', 'evidence',
                                  'owner', 'generation', 'expired', 'revoked', 'scope', 'plan_changed',
                                  'hint_replaced', 'same_text_replaced', 'catalog_changed', 'mandatory_late', 'registration', 'unload'])
def test_removal_consumer_rechecks_exact_identity_and_vetoes(native, monkeypatch, case):
    messages, items, binding, entry = ready(native)
    hint, task, plan, registration = entry
    before = dict(binding.views.skills.hints)
    if case == 'remaining':
        native.mode['remaining'] = .99
    proposals = []
    submit = native.facade.submit
    def intercept(p):
        p = copy.deepcopy(p)
        if p['metadata']['feature_action'] == 'remove_own_hint':
            if case == 'hint_id': p['metadata']['hint_id'] = 'foreign-hint'
            elif case == 'feature_action': p['metadata']['feature_action'] = 'rank_skill_ids'
            elif case == 'feature_id': p['feature_id'] = 'F13'
            elif case == 'candidate': p['candidate_ids'] = ['alpha']
            elif case == 'evidence': p['evidence_refs'] = ['foreign']
            elif case == 'owner': p['owner'] = 'foreign'
            elif case == 'generation': p['plugin_generation'] = 'foreign'
            elif case == 'expired': monkeypatch.setattr(native.runtime, 'clock', lambda: native.runtime.round_deadline + 1)
            elif case == 'revoked': registration.active = False
            elif case == 'unload': native.facade.unregister()
            elif case == 'same_text_replaced': binding.views.skills.rank((hint.skill_id,), revision=hint.catalog_revision, plugin_id=hint.plugin_id, ambiguous=True)
            elif case == 'scope': native.runtime.revision = replace(native.runtime.revision, instruction_event=999)
            elif case == 'plan_changed': native.agent._todo_store.write([{'id': 'new', 'content': 'Unfinished', 'status': 'pending'}])
            elif case == 'hint_replaced': binding.skill_hints[hint.plugin_id] = tuple(list(entry))
            elif case == 'catalog_changed': binding.views.skills.revision = 'changed'
            elif case == 'mandatory_late': binding.views.skills.candidates = tuple(replace(c, required=True) for c in binding.views.skills.candidates)
            elif case == 'registration': binding.skill_hints[hint.plugin_id] = (hint, task, plan, object())
            proposals.append(p)
        return submit(p)
    monkeypatch.setattr(native.facade, 'submit', intercept)
    # Reopening the actual store during settlement intentionally differs from the committed result.
    if case == 'plan_changed':
        # Use the normal executor too; assertion belongs below, not the helper's equality check.
        a = native.agent
        a.tools = _make_tool_defs('todo_list')
        call = _mock_tool_call(name='todo_list', call_id='finish')
        call.function.arguments = json.dumps({'todos': [{**t, 'status': 'completed'} for t in items]})
        messages.append({'role': 'assistant', 'content': '', 'tool_calls': [
            {'id': call.id, 'type': 'function', 'function': {'name': 'todo_list', 'arguments': call.function.arguments}}]})
        a._execute_tool_calls_sequential(SimpleNamespace(content='', tool_calls=[call]), messages, 'task')
    else:
        todo(native, messages, [{**t, 'status': 'completed'} for t in items])
    native.drain()
    assert len(removal_calls(native)) == 1
    if case == 'unload':
        assert not binding.skill_hints  # lifecycle reset is not an applied feature effect
    else:
        assert binding.views.skills.hints == before
    for p in proposals:
        receipt = native.runtime.receipts.get(p['proposal_id'])
        assert receipt is None or receipt.status != 'applied'


@pytest.mark.parametrize('phase', ['old_task', 'uncommitted', 'empty', 'oversized'])
def test_old_task_plan_is_not_current_phase_authority(native, phase):
    if phase == 'old_task':
        messages, items, binding, _ = ready(native)
        native.accept('Analyze weather observations.')  # new task, old store retained
    else:
        skill_catalog(native)
        messages = [{'role': 'user', 'content': 'Analyze weather observations.'}]
        binding = native.agent._supervision_view_binding
        items = [{'id': 'one', 'content': 'Analyze weather observations.', 'status': 'in_progress'}]
        if phase == 'uncommitted':
            native.agent._todo_store.write(items)
        elif phase == 'oversized':
            items = [{**items[0], 'id': str(i)} for i in range(9)]
            todo(native, messages, items)
    assemble(native.agent, messages)
    entry, = binding.skill_hints.values()
    assert entry[2] is None
    before = dict(binding.views.skills.hints)
    todo(native, messages, [{**t, 'status': 'completed'} for t in items])
    native.drain()
    assert not removal_calls(native)
    assert binding.views.skills.hints == before
    assemble(native.agent, messages)
    calls = len(native.calls)
    todo(native, messages)
    assemble(native.agent, messages)
    assert not removal_calls(native)
    assert len(native.calls) == calls


@pytest.mark.parametrize('event', ['same_completed', 'changed_phase', 'reopened', 'reopened_renamed',
                                  'changed_completed', 'deleted_completed', 'catalog',
                                  'steering', 'task', 'unload', 'uncommitted_plan'])
def test_retirement_reopens_only_for_relevant_native_events(native, event):
    messages, items, binding, entry = ready(native)
    original_hint = entry[0]
    finished = [{**t, 'status': 'completed'} for t in items]
    todo(native, messages, finished)
    assemble(native.agent, messages)
    assert not binding.views.skills.hints
    calls = len(native.calls)
    previous_task_scope = binding.skill_scope
    previous_phase = binding.skill_phase
    def new_catalog():
        path = native.home / 'skills' / 'epsilon' / 'SKILL.md'
        path.parent.mkdir()
        path.write_text('---\nname: epsilon\ndescription: Analyze weather observations and temperature.\n---\nCompare weather reports.\n')
    def steering():
        from agent.supervision_context import accepted_input_origin
        assert native.agent.steer('Analyze weather forecasts.', origin=accepted_input_origin(
            'Analyze weather forecasts.', kind='cli', continuation=True))
        todo(native, messages)  # deliver through the ordinary tool-round boundary
    transitions = {
        'same_completed': lambda: todo(native, messages, finished),  # store revision alone is not a new phase
        'changed_phase': lambda: todo(native, messages, [
            {'id': 'next', 'content': 'Analyze another weather report.', 'status': 'pending'}]),
        'reopened': lambda: todo(native, messages, items),
        'reopened_renamed': lambda: todo(native, messages, [{**t, 'id': 'new-' + t['id']} for t in items]),
        'changed_completed': lambda: todo(native, messages, [{**t, 'content': 'Changed ' + t['content']} for t in finished]),
        'deleted_completed': lambda: todo(native, messages, finished[:1]),
        'catalog': new_catalog,
        'steering': steering,
        'task': lambda: native.accept('Analyze weather forecasts.'),
        'unload': native.facade.unregister,
        'uncommitted_plan': lambda: native.agent._todo_store.write(items),  # no committed producer
    }
    transitions[event]()
    history = copy.deepcopy(messages)
    result = assemble(native.agent, messages)
    native.drain()
    reopened = event in {'changed_phase', 'reopened', 'reopened_renamed', 'catalog', 'steering', 'task'}
    assert bool(binding.skill_contents()) is reopened
    presented = native.mode['presented_skill_rows']
    assert result.api_messages[:len(presented)] == presented
    assert sum(original_hint.content in row.get('content', '') for row in result.api_messages) == 1
    assert len(native.calls) == calls + (2 if reopened else 0)
    assert messages == history
    assert len(removal_calls(native)) == 1
    if reopened:
        new, = binding.skill_hints.values()
        assert new[0].id != original_hint.id
        assert new[0].plugin_id == original_hint.plugin_id
        assert new[0].registration == original_hint.registration
        if event in {'steering', 'task'}:
            assert binding.skill_scope != previous_task_scope
            assert new[2] is None  # previous task's plan cannot be rebound
        if event in {'changed_phase', 'reopened', 'reopened_renamed'}:
            assert binding.skill_phase != previous_phase
            assert new[2] == native.agent._todo_store.snapshot()
    else:
        assert not binding.skill_hints
    assemble(native.agent, messages)
    assert len(native.calls) == calls + (2 if reopened else 0)


@pytest.mark.parametrize('stage', ['rank_skill_ids', 'remove_own_hint'])
@pytest.mark.parametrize('status', ['accepted', 'selected', 'applied'])
def test_skill_effects_fail_closed_when_real_receipt_writer_is_locked(native, monkeypatch, stage, status):
    import sqlite3
    from agent.supervision_receipts import lookup
    items, entry = [], None
    if stage == 'remove_own_hint':
        messages, items, binding, entry = ready(native)
        before = dict(binding.views.skills.hints)
    else:
        skill_catalog(native)
        messages = [{'role': 'user', 'content': 'Analyze weather observations.'}]
        binding = native.agent._supervision_view_binding
        before = {}
    blocked = []
    settle = native.runtime._settle
    def locked(proposal, disposition, reason):
        if proposal.metadata.get('feature_action') != stage or disposition != status:
            return settle(proposal, disposition, reason)
        # A real second connection blocks the canonical zero-wait writer. No DDL,
        # fake persistence success, or replacement SessionDB/storage implementation.
        with sqlite3.connect(native.agent._session_db.db_path, timeout=0) as conn:
            conn.execute('BEGIN IMMEDIATE')
            receipt = settle(proposal, disposition, reason)
        blocked.append((proposal, receipt))
        return receipt
    monkeypatch.setattr(native.runtime, '_settle', locked)
    if stage == 'remove_own_hint':
        todo(native, messages, [{**t, 'status': 'completed'} for t in items])
    else:
        assemble(native.agent, messages)
    native.drain()
    proposal, receipt = blocked[0]
    assert len(blocked) == 1
    assert receipt.reason == 'storage_unavailable' and receipt.status != 'applied'
    assert binding.views.skills.hints == before
    persisted = lookup(native.runtime, proposal.proposal_id)
    assert persisted is None or persisted[0] != 'applied'
    if stage == 'remove_own_hint':
        assert entry is not None
        assert binding.skill_hints[entry[0].plugin_id] is entry
        assert binding.views.skills.owned_hints[entry[0].plugin_id] is entry[0]
    calls = len(native.calls)
    result = assemble(native.agent, messages)
    assert binding.views.skills.hints == before
    body = (native.home / 'skills/alpha/SKILL.md').read_text()
    assert any(body in row.get('content', '') for row in result.api_messages) is bool(before)
    assert bool(binding.skill_contents()) is bool(before)
    assert len(native.calls) == calls


def test_ordinary_unhinted_todo_completion_is_silent(native):
    native.accept('Ordinary local task.')
    messages = []
    todo(native, messages, [{'id': 'one', 'content': 'Done', 'status': 'completed'}])
    native.drain()
    assert not native.calls


@pytest.mark.parametrize('bookkeeping', ['reorder', 'ids'])
def test_retired_hint_stays_retired_after_completed_bookkeeping(native, record_property, bookkeeping):
    messages, items, binding, entry = ready(native)
    completed = [{**t, 'status': 'completed'} for t in items]
    todo(native, messages, completed)
    result = assemble(native.agent, messages)
    presented = native.mode['presented_skill_rows']
    assert result.api_messages[:len(presented)] == presented
    assert entry[0].content not in result.api_messages[-1]['content']
    assert not binding.skill_contents()
    assert len(removal_calls(native)) == 1
    phase = binding.skill_phase
    changed = (list(reversed(completed)) if bookkeeping == 'reorder' else
               [{**t, 'id': 'renamed-' + t['id']} for t in completed])
    assert sorted((t['content'], t['status']) for t in changed) == sorted(
        (t['content'], t['status']) for t in completed)
    todo(native, messages, changed)
    history = copy.deepcopy(messages)
    result = assemble(native.agent, messages)
    native.drain()
    assert [b['state']['facts']['stage'] for b, _ in native.calls] == ['metadata', 'detail', 'unload']
    presented = native.mode['presented_skill_rows']
    assert result.api_messages[:len(presented)] == presented
    assert entry[0].content not in result.api_messages[-1]['content']
    assert not binding.skill_contents()
    assert binding.skill_phase == phase
    record_property('retirement', dict(bookkeeping=bookkeeping, old_hint=entry[0].id,
        stages=[b['state']['facts']['stage'] for b, _ in native.calls], next_request=result.api_messages))
    assert not binding.skill_hints
    assert len(removal_calls(native)) == 1
    assert messages == history
