"""Ordinary committed annex + native LCM hydration + full Jev bridge -> durable contest.

No constructed source propositions, owner edges, snapshots or success proposals.
The synthetic transport qualifies plumbing, not Jev accuracy or correction delivery.
"""
import copy
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

import hermes_lcm
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from hermes_state import SessionDB
from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_context import digest
from agent.supervision_literal_sources import VERSION
from agent.supervision_planning_records import load
from agent.supervision_types import Action
from tests.agent.supervision_test_support import Agent, accept
from tests.agent.test_supervision_literal_sources import record
from tests.agent.test_supervision_native_relations import FixtureCredential, write
from tools.todo_tool import todo_tool
from jev_supervisor.config import Config
from jev_supervisor.host_adapter import NativeHostBridge
from jev_supervisor.transport import Transport


@pytest.fixture
def native(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('LCM_DATABASE_PATH', str(home / 'lcm.db'))
    monkeypatch.setenv('LCM_EMBEDDINGS_ENABLED', 'false')
    monkeypatch.setenv('LCM_ASSERTIONS_ENABLED', 'false')
    def denied(*args, **kwargs):
        raise AssertionError('no network/DNS or additional retrieval')
    monkeypatch.setattr('socket.getaddrinfo', denied)
    monkeypatch.setattr('socket.socket.connect', denied)
    from hermes_lcm import tools
    monkeypatch.setattr(tools, 'lcm_recall', denied)
    fields = ('target_id a b comparability dependent_ids deterministic_invalidation delivered_consequential '
        'changed immutable_verified claim linked_requirement_id evidence_contract deterministic_contract candidates').split()
    policy = dict(id='claim-fixture', profile=str(home), fields={k: 'synthetic' for k in fields}, sources={}, fixture=True)
    config = {'supervision': {'enabled': True, 'plugins': {
        'hermes-lcm': {'literal_sources': {'version': VERSION, 'recipients': ['fixture-claims']}},
        'fixture-claims': {'grants': ['observe', Action.CONTEST_CLAIM.value, 'update_dependencies', 'continue'],
            'data_policy': ['history_excerpt', 'task_text', 'project_excerpt'], 'egress_policy': policy}}}}
    (home / 'config.yaml').write_text(json.dumps(config))
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='hermes-lcm'), manager)
    hermes_lcm.register(ctx)
    prototype = manager._context_engine
    engine = prototype.clone_for_agent()
    engine.on_session_start('session-A', platform='cli')
    facade = PluginContext(PluginManifest(name='fixture-claims'), manager).supervision
    calls, mode = [], {'choice': 'incompatible'}
    async def respond(request):
        assert str(request.url) == 'https://api.typesafe.ai/v1/systemone'
        body = json.loads(request.content)
        assert set(body) == {'model', 'state', 'questions'} and body['model'] == 'jev-1.13.0'
        assert all(k.startswith(('F22/', 'F17/')) for k in body['questions'])
        source = ctx.supervision._literal_source_registration
        for lock in (rt.lock, facade._registration.fence, source.lock, source.provider.lock):
            assert lock.acquire(blocking=False), 'external I/O under an authority lock'
            lock.release()
        calls.append(body)
        if mode.get('on_response'):
            mode.pop('on_response')()
        answers = {}
        for key, q in body['questions'].items():
            choice = 'supports' if key.startswith('F17/') else mode['choice']
            answers[key] = dict(type='choice', choice=choice, confidence=.99,
                probabilities={k: float(k == choice) for k in q['criteria']})
        return httpx.Response(200, json=dict(model=body['model'], usage={}, answers=answers))
    cfg = Config(str(home), True, policy_id=policy['id'], allowed_classes={'synthetic'}, fixture_policy=True)
    bridge = NativeHostBridge(facade, cfg, None,
        transport=Transport(cfg, FixtureCredential(str(home)), http_transport=httpx.MockTransport(respond)))
    assert bridge.start()
    agent = Agent()
    agent.context_compressor = engine
    db = agent._session_db = SessionDB(home / 'state.db')
    db.create_session(agent.session_id, 'cli')
    plan, claim_file = tmp_path / 'plan.md', tmp_path / 'claim.md'
    rt = accept(agent, f'- Maintain `{plan}`.\n- Answer using `{claim_file}`.', message_id='user-claim')
    reads = []
    original = engine._store.get
    def get(sid):
        assert not rt.lock._is_owned(), 'source I/O under graph lock'
        reads.append(sid)
        return original(sid)
    monkeypatch.setattr(engine._store, 'get', get)
    n = SimpleNamespace(home=home, manager=manager, ctx=ctx, facade=facade, bridge=bridge,
        engine=engine, agent=agent, rt=rt, calls=calls, mode=mode, plan=plan, claim_file=claim_file, reads=reads)
    yield n
    rt.revoke()
    bridge.close()
    ctx.supervision.unregister()
    engine.shutdown()
    prototype.shutdown()
    db.close()
    assert not bridge._thread.is_alive()


def append(n, data):
    text = json.dumps(data, ensure_ascii=False)
    sid = n.engine._store.append('session-A', dict(role='user', content=text, timestamp=1700000000))
    return sid, text, f'lcm:{sid}:0-{len(text)}'


def pack(n, sources, mode='pack'):
    args = dict(question='What is the mass of asset A?', baseline_refs=[dict(exact_ref=r[2]) for r in sources],
                budgets=dict(max_retrieval_calls=0))
    name = 'lcm_evidence_pack'
    if mode != 'pack':
        name = 'lcm_compile_evidence'
        args['mode'] = mode
        if mode == 'proposal':
            args['proposal'] = dict(version='evidence-selector-v1', requested_facets=[], missing_facets=[],
                selections=[dict(claim_id=f'c{i}', facet='answer', exact_ref=r[2], quote=r[1]) for i, r in enumerate(sources)])
    with bind_subagent_parent(n.agent):
        result = json.loads(n.engine.handle_tool_call(name, args))
    # Real committed tool-round boundary; future publications cannot reuse its grants.
    n.rt.committed_batch([])
    return result


def prepare(n, *, variant=None, other=None, count=1, first_data=None):
    first_data = first_data or record()
    first = append(n, first_data)
    second_data = copy.deepcopy(first_data)
    second_data['value'] = 4
    if other:
        other(second_data)
    second = append(n, second_data)
    initial = pack(n, [first])
    assert len(initial['source_propositions']) == 1
    with bind_subagent_parent(n.agent):
        todo = json.loads(todo_tool([dict(id='answer', content=f'Prepare `{n.plan}` and `{n.claim_file}`',
            status='in_progress')], store=n.agent._todo_store))
    requirement = todo['work_map_sources']['requirements'][1]
    # User/model-side rendering built from exact returned source bytes, not from
    # a constructed host SourceProposition or model-extracted semantic fields.
    claim = 'LCM record ' + first[2] + ': ' + json.dumps(first[1], ensure_ascii=True)
    if variant == 'paraphrase':
        claim = 'Asset A has mass 3 kg.'
    body = {'quote': '> ', 'rejected': 'I reject this source: '}.get(variant, '') + claim
    if variant == 'duplicate':
        body += '\n' + claim
    write(n, n.claim_file, body)
    start = body.index(claim)
    wm = dict(version=1, requirements=[{k: requirement[k] for k in ('source_message_id', 'start', 'end')}],
        steps=[dict(todo_id='answer', requirement_indexes=[0], target_refs=[str(n.claim_file)])],
        claims=[dict(artifact_ref=str(n.claim_file), start=start, end=start+len(claim), requirement_indexes=[0])])
    req = n.rt.sources[requirement['source_message_id']]
    declaration = dict(type='claim_use', local_id='adopt', use='answer_dependency', source_ref=first[2],
        claim_ref=dict(source_ref=str(n.claim_file), source_revision=digest(body), start=start, end=start+len(claim)),
        requirement_refs=[dict(source_ref=requirement['source_message_id'], source_revision=digest(req),
                              start=requirement['start'], end=requirement['end'])])
    if variant == 'unknown-key':
        declaration['consequential'] = True
    if variant == 'incidental':
        declaration['use'] = 'citation'
    if variant == 'unlinked':
        wm['claims'] = []
    rows = [{**declaration, 'local_id': f'adopt-{i}'} for i in range(count)]
    content = ('```hermes-work-map-v1\n' + json.dumps(wm) + '\n```\n'
        + '```hermes-planning-proposals-v1\n' + json.dumps(dict(version=1, records=rows)) + '\n```\n')
    write(n, n.plan, content)
    n.rt.committed_batch([])
    return first, second


@pytest.mark.parametrize('mode', ['pack', 'proposal', 'auto'])
def test_ordinary_source_to_durable_contest_no_truth_or_delivery(native, mode):
    n = native
    sources = prepare(n)
    before = load(n.rt)
    assert len(before) == 1 and before[0]['status'] == 'claim_declared'
    result = pack(n, sources, mode)
    after = load(n.rt)
    assert len(n.calls) == 1, (n.calls, result, n.bridge.supervisor.inspect(), list(n.rt.receipts.values()))
    assert after[0]['status'] == 'contested', (after, list(n.rt.receipts.values()))
    assert after[0]['truth_winner'] is None and after[0]['suspend_optional_reuse'] is True
    assert after[0]['emission'] == after[0]['correction_relevance'] == 'unknown'
    assert after[0]['source_refs'] == result['source_propositions']
    assert [r['exact_ref'] for r in after[0]['source_identities']] == [s[2] for s in sources]
    assert after[0]['claim_id'] in n.rt.dependencies.contested_claims
    facts = n.calls[0]['state']['facts']
    assert facts['a']['text'] == sources[0][1] and facts['b']['text'] == sources[1][1]
    assert facts['comparability']['predicate'] is True and facts['delivered_consequential'] is False
    assert any(r.status == 'applied' for r in n.rt.receipts.values())
    assert set(n.reads) == {s[0] for s in sources}
    assert n.rt.final_continuations == 0 and not getattr(n.rt, '_native_emission_scope', None)
    assert 'focused_correction' in n.bridge.inspect()['unsupported_actions']
    # Graph reload and new ordinary hydration must not reclassify the same claim.
    n.rt.dependencies.planning.clear()
    pack(n, sources, mode)
    assert len(n.calls) == 1 and load(n.rt) == after


@pytest.mark.parametrize('axis', ['entity', 'predicate', 'time', 'scope', 'quantity'])
def test_different_coordinate_abstains_without_http(native, axis):
    def change(data):
        if axis == 'time':
            data[axis]['end'] = '2026-03-01T00:00:00Z'
        elif axis == 'quantity':
            data[axis]['unit'] = 'm'
        else:
            data[axis]['namespace'] = 'other'
    sources = prepare(native, other=change)
    pack(native, sources)
    assert not native.calls and load(native.rt)[0]['status'] == 'claim_declared'


@pytest.mark.parametrize('variant', ['paraphrase', 'quote', 'rejected', 'duplicate', 'unknown-key', 'incidental', 'unlinked'])
def test_no_unambiguous_whole_record_adoption(native, variant):
    pack(native, prepare(native, variant=variant))
    assert not native.calls
    assert not native.rt.dependencies.contested_claims
    assert all(n['status'] == 'claim_declared' for n in load(native.rt))


@pytest.mark.parametrize('choice', ['compatible', 'different_scope', 'insufficient'])
def test_real_plugin_noop_is_not_contest(native, choice):
    native.mode['choice'] = choice
    sources = prepare(native)
    pack(native, sources)
    pack(native, sources)
    assert len(native.calls) == 1
    assert load(native.rt)[0]['status'] == 'claim_declared'
    assert not native.rt.dependencies.contested_claims


def test_affected_scope_overflow_is_unknown_not_prefix(native):
    pack(native, prepare(native, count=5))
    assert not native.calls and len(load(native.rt)) == 5
    assert not native.rt.dependencies.contested_claims


@pytest.mark.parametrize('failure', ['source-grant', 'recipient-grant', 'source-row', 'revision', 'delete'])
def test_post_inference_revalidation_vetoes_stale_contest(native, failure):
    n = native
    sources = prepare(n)
    def revoke():
        if failure == 'source-grant':
            n.ctx.supervision._literal_source_registration.recipients = frozenset()
        elif failure == 'recipient-grant':
            n.facade._registration.grants = frozenset({'observe'})
        elif failure == 'source-row':
            n.engine._store._conn.execute("UPDATE messages SET role='assistant' WHERE store_id=?", (sources[0][0],))
            n.engine._store._conn.commit()
        elif failure == 'revision':
            from dataclasses import replace
            with n.rt.lock:
                n.rt.revision = replace(n.rt.revision, evidence=n.rt.revision.evidence + 1)
        else:
            n.agent._session_db.delete_session(n.agent.session_id)
    n.mode['on_response'] = revoke
    pack(n, sources)
    assert len(n.calls) == 1
    assert not n.rt.dependencies.contested_claims
    assert all(row['status'] != 'contested' for row in load(n.rt))


def test_busy_canonical_writer_never_claims_contest(native):
    sources = prepare(native)
    conn = sqlite3.connect(native.agent._session_db.db_path, timeout=0)
    try:
        conn.execute('BEGIN IMMEDIATE')
        pack(native, sources)
    finally:
        conn.rollback()
        conn.close()
    assert not native.rt.dependencies.contested_claims
    assert load(native.rt)[0]['status'] == 'claim_declared'


@pytest.mark.parametrize('change', ['source-grant', 'predecessor-status', 'todo'])
def test_writer_revalidates_after_last_source_read(native, monkeypatch, change):
    from agent import supervision_planning_records as records
    n = native
    sources = prepare(n)
    original = records.save
    attempts = []
    def save(rt, rows, **kwargs):
        if any(r['status'] == 'contested' for r in rows):
            attempts.append(True)
            if change == 'source-grant':
                n.ctx.supervision._literal_source_registration.recipients = frozenset()
            elif change == 'predecessor-status':
                n.agent._session_db._conn.execute("UPDATE supervision_owner_records SET status='stale'")
                n.agent._session_db._conn.commit()
            else:
                with bind_subagent_parent(n.agent):
                    todo_tool([dict(id='answer', content='Changed current step', status='completed')],
                              store=n.agent._todo_store)
        return original(rt, rows, **kwargs)
    monkeypatch.setattr(records, 'save', save)
    pack(n, sources)
    assert attempts and len(n.calls) == 1
    assert not n.rt.dependencies.contested_claims
    assert all(r['status'] != 'contested' for r in load(n.rt))


def test_source_revocation_before_send_has_zero_http(native, monkeypatch):
    n = native
    sources = prepare(n)
    original = n.facade.admit_dispatch
    checks = []
    def admit(request):
        checks.append(True)
        n.ctx.supervision._literal_source_registration.recipients = frozenset()
        return original(request)
    monkeypatch.setattr(n.facade, 'admit_dispatch', admit)
    pack(n, sources)
    assert checks and not n.calls
    assert not n.rt.dependencies.contested_claims


def test_native_supported_edge_loses_optional_reuse_after_contest(native):
    n = native
    source_file = n.plan.parent / 'source.md'
    data = record()
    data['quantity'] = dict(kind='non_quantity', unit=None)
    data['value'] = f'See `{source_file}`.'
    sources = prepare(n, first_data=data, other=lambda d: d.update(value='Alternative assertion'))
    # Declare a current todo target through the ordinary native owner, then
    # recommit the plan so both work-map and claim-use bind that exact revision.
    with bind_subagent_parent(n.agent):
        todo_tool([dict(id='answer', content=f'Prepare `{n.plan}`, `{n.claim_file}` and `{source_file}`',
                       status='in_progress')], store=n.agent._todo_store)
    write(n, source_file, sources[0][1])
    # A different local id avoids rebinding the old declaration's identity.
    content = n.rt.artifacts[str(n.plan)].replace('adopt-0', 'adopt-current')
    write(n, n.plan, content)
    n.rt.committed_batch([])
    claim = n.rt.artifacts[str(n.claim_file)]
    assert n.rt.prepare_final(claim) is None
    edges = tuple(n.rt.dependencies.edges.values())
    assert len(edges) == 1 and n.rt.dependencies.optional_support(edges[0].id) is not None
    n.rt.committed_batch([])
    pack(n, sources)
    assert n.rt.dependencies.optional_support(edges[0].id) is None
    assert any(r['status'] == 'contested' for r in load(n.rt))
    assert len([c for c in n.calls if any(k.startswith('F22/') for k in c['questions'])]) == 1
    n.rt.dependencies.planning.clear()
    n.rt.dependencies.contested_claims.clear()
    assert n.rt.dependencies.optional_support(edges[0].id) is None
