"""Committed native source use -> F14 judgment -> actual recall bytes and receipt.

No premise setters, synthetic owner projections, or constructed proposals. HTTP
answers qualify plumbing only; literal records and all homes/DBs are synthetic.
"""
import asyncio
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

import hermes_lcm
from agent.subagent_lifecycle import bind_subagent_parent
from agent.supervision_literal_sources import VERSION, WORKING_PREMISE_VERSION
from agent.supervision_planning_records import load
from agent.supervision_retrieval_presentation import FIELD
from hermes_cli.plugins import PluginContext, PluginManager
from hermes_cli.plugins_manifest import PluginManifest
from hermes_state import SessionDB
from jev_supervisor.config import Config
from jev_supervisor.host_adapter import NativeHostBridge
from jev_supervisor.transport import Transport
from tests.agent.supervision_test_support import Agent, accept
from tests.agent.test_supervision_claim_uses import prepare, append
from tests.agent.test_supervision_literal_sources import record
from tests.agent.test_supervision_native_relations import FixtureCredential


@pytest.fixture
def native(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setenv('LCM_DATABASE_PATH', str(home / 'lcm.db'))
    monkeypatch.setenv('LCM_EMBEDDINGS_ENABLED', 'false')
    monkeypatch.setenv('LCM_ASSERTIONS_ENABLED', 'false')
    def denied(*a, **kw):
        raise AssertionError('network/DNS forbidden')
    monkeypatch.setattr('socket.getaddrinfo', denied)
    monkeypatch.setattr('socket.socket.connect', denied)
    fields = ('query candidates scope required_ids baseline_ids needs_triage exact_answer_complete target_id premise').split()
    policy = dict(id='working-premise-fixture', profile=str(home), fixture=True,
                  fields={k: 'synthetic' for k in fields}, sources={'premise': WORKING_PREMISE_VERSION})
    config = {'supervision': {'enabled': True, 'plugins': {
        'hermes-lcm': {'literal_sources': {'version': VERSION, 'recipients': ['fixture-working']},
                       'working_premises': {'version': WORKING_PREMISE_VERSION, 'recipients': ['fixture-working']}},
        'fixture-working': {'grants': ['observe', 'rank_candidates'],
            'data_policy': ['history_excerpt', 'task_text', 'project_excerpt'], 'egress_policy': policy}}}}
    (home / 'config.yaml').write_text(json.dumps(config))
    manager = PluginManager(scope_key=str(home))
    ctx = PluginContext(PluginManifest(name='hermes-lcm'), manager)
    hermes_lcm.register(ctx)
    prototype = manager._context_engine
    engine = prototype.clone_for_agent()
    engine.on_session_start('session-A', platform='cli')
    facade = PluginContext(PluginManifest(name='fixture-working'), manager).supervision
    calls, mode = [], {}
    async def respond(request):
        assert str(request.url) == 'https://api.typesafe.ai/v1/systemone'
        body = json.loads(request.content)
        assert set(body) == {'model', 'state', 'questions'} and body['model'] == 'jev-1.13.0'
        assert all(k.startswith('F14/') for k in body['questions'])  # no duplicate F22
        source = ctx.supervision._literal_source_registration
        for lock in (rt.lock, facade._registration.fence, source.lock, source.provider.lock):
            assert lock.acquire(blocking=False), 'external I/O under an authority lock'
            lock.release()
        calls.append(body)
        await asyncio.sleep(.005)  # first-seen response, not a synchronous fixture callback
        if mode.get('on_response'):
            mode.pop('on_response')()
        rows = body['state']['facts']['candidates']
        answers = {}
        for key in body['questions']:
            # Conflict source also has low direct relevance; preserve it anyway.
            chosen = rows[0]['id'] if 'conflict:' in key else rows[2]['id']
            answers[key] = dict(type='noul', noul=.99 if key.endswith(chosen) else .01)
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
    rt = accept(agent, f'- Maintain `{plan}` and `{claim_file}`.\n- mass', message_id='user-working')
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


def seed(n, variant=None, other=None):
    # More plausible hits than the native rank budget, retaining whole records.
    for i in range(8):
        data = record()
        data['value'] = i + 10
        append(n, data)
    return prepare(n, variant=variant, other=other)


def recall(n, query='- mass'):
    with bind_subagent_parent(n.agent):
        return n.engine.handle_tool_call('lcm_recall', dict(query=query, limit=10, detail='answer_ready'))


def consumed(n):
    return [r.reason for r in n.rt.receipts.values() if r.status == 'applied']


def test_committed_working_use_reaches_real_native_conflict_and_receipt(native, monkeypatch, record_property):
    n = native
    sources = seed(n)
    before = load(n.rt)
    old_publications = dict(n.ctx.supervision._literal_source_registration.records)
    assert len(before) == 1 and before[0]['status'] == 'claim_declared'
    from hermes_lcm import recall_response
    shape = recall_response.shape_recall_response
    baselines = []
    def shaped(**kw):
        result = shape(**kw)
        if kw['rerank_status'] == 'disabled':
            baselines.append(result)
        return result
    monkeypatch.setattr(recall_response, 'shape_recall_response', shaped)
    encoded = recall(n)
    assert len(n.calls) == 1, (n.calls, n.bridge.supervisor.inspect(), list(n.rt.receipts.values()))
    facts = n.calls[0]['state']['facts']
    assert facts['premise'] == sources[0][1]  # every original qualifier, not query text
    assert facts['query'] != facts['premise']
    conflict_questions = [q for k, q in n.calls[0]['questions'].items() if 'conflict:' in k]
    assert len(conflict_questions) == len(facts['candidates'])
    assert all(q['instructions']['target']['premise'] == sources[0][1] for q in conflict_questions)
    actual, baseline = json.loads(encoded), json.loads(baselines[0])
    block = actual[FIELD]['conflicts']
    assert 'No truth winner' in block['notice']
    assert sorted(actual['hits'], key=lambda h: h['store_id']) == sorted(baseline['hits'], key=lambda h: h['store_id'])
    assert actual['hits'] != baseline['hits']
    ref, = block['sources']
    assert actual['hits'][int(ref['source_pointer'].split('/')[-1])]['content'] == facts['candidates'][0]['excerpt']
    digest = hashlib.sha256(encoded.encode()).hexdigest()
    assert consumed(n) == ['owner_consumed:' + digest]
    with n.agent._session_db._lock:
        durable = n.agent._session_db._conn.execute("SELECT status, reason FROM supervision_receipts WHERE status='consumed'").fetchall()
    assert [tuple(r) for r in durable] == [('consumed', 'owner_consumed:' + digest)]
    assert load(n.rt) == before  # still WORKING, never accepted-final or contested by F14
    assert not n.rt.owner_selections and not n.rt.dependencies.contested_claims
    # Old publication refs were invalidated by the ordinary committed batch.
    source = n.ctx.supervision._literal_source_registration
    assert source.records == old_publications  # no renewed/new v1 publication
    assert all(source.lookup(n.rt, ref, n.facade._registration) is None for ref in old_publications)
    record_property('returned_utf8', encoded)
    record_property('wire_request', json.dumps(n.calls[0]))
    record_property('returned_sha256', digest)


@pytest.mark.parametrize('failure', ['working_grant', 'egress_source', 'egress_field', 'query', 'paraphrase', 'unlinked', 'scope', 'qualifier', 'busy'])
def test_unavailable_working_use_keeps_query_only(native, monkeypatch, failure):
    n = native
    other = (lambda d: d['scope'].update(id='other')) if failure == 'scope' else None
    if failure == 'qualifier':
        other = lambda d: d.pop('modality')
    seed(n, variant=failure if failure in {'paraphrase', 'unlinked'} else None, other=other)
    source, reg = n.ctx.supervision._literal_source_registration, n.facade._registration
    if failure == 'working_grant':
        source.working_recipients = frozenset()
    elif failure in {'egress_source', 'egress_field'}:
        from agent.supervision_types import project, freeze
        policy = project(reg.egress_policy)
        policy['sources'].pop('premise')
        if failure == 'egress_field':
            policy['fields'].pop('premise')
        reg.egress_policy = freeze(policy)
    if failure == 'busy':
        monkeypatch.setattr(source.provider, 'working_literal_source', lambda *a: None)
    encoded = recall(n, query='mass' if failure == 'query' else '- mass')
    assert len(n.calls) == 1, n.bridge.supervisor.inspect()
    assert 'premise' not in n.calls[0]['state']['facts']
    assert not any('conflict:' in k for k in n.calls[0]['questions'])
    assert 'conflicts' not in json.loads(encoded).get(FIELD, {})
    assert not n.rt.owner_selections


@pytest.mark.parametrize('failure', ['source', 'working_grant', 'egress', 'class', 'claim', 'revision', 'deadline', 'lifecycle', 'source_busy', 'canonical_busy', 'oversize'])
def test_ack_revalidates_original_working_source_and_authority(native, monkeypatch, failure):
    n = native
    sources = seed(n)
    acknowledge = n.rt.acknowledge_owner
    reached = []
    baselines = []
    from hermes_lcm import recall_response
    shape = recall_response.shape_recall_response
    def shaped(**kw):
        result = shape(**kw)
        if kw['rerank_status'] == 'disabled':
            baselines.append(result)
        return result
    monkeypatch.setattr(recall_response, 'shape_recall_response', shaped)
    def changed(*args):
        assert any(r.reason == 'owner_selected' for r in n.rt.receipts.values())
        reached.append(True)
        if failure in {'source_busy', 'canonical_busy'}:
            return busy_ack(n, acknowledge, args, failure)
        if failure == 'oversize':
            n.engine._store._conn.execute("UPDATE messages SET content=? WHERE store_id=?", ('x' * 2401, sources[0][0]))
            n.engine._store._conn.commit()
        elif failure == 'source':
            n.engine._store._conn.execute("UPDATE messages SET role='assistant' WHERE store_id=?", (sources[0][0],))
            n.engine._store._conn.commit()
        elif failure == 'working_grant':
            n.ctx.supervision._literal_source_registration.working_recipients = frozenset()
        elif failure == 'egress':
            n.facade._registration.egress_policy = {}
        elif failure == 'class':
            n.facade._registration.data_policy -= {'task_text'}
        elif failure == 'claim':
            n.rt.artifacts[str(n.claim_file)] = 'changed'
        elif failure == 'revision':
            n.rt.revision = replace(n.rt.revision, evidence=n.rt.revision.evidence + 1)
        elif failure == 'deadline':
            # Deterministic final-edge expiry; never enlarges a production budget.
            n.rt.clock = lambda: n.rt.round_deadline + 1
        else:
            n.engine.on_session_start('other', platform='cli')
        return acknowledge(*args)
    monkeypatch.setattr(n.rt, 'acknowledge_owner', changed)
    encoded = recall(n)
    assert reached and len(n.calls) == 1
    assert 'premise' in n.calls[0]['state']['facts']
    assert encoded == baselines[0] and FIELD not in json.loads(encoded) and not consumed(n)
    assert not n.rt.owner_selections


def busy_ack(n, acknowledge, args, failure):
    import sqlite3
    import threading
    acquired, release = threading.Event(), threading.Event()
    def hold():
        if failure == 'source_busy':
            with n.engine._store._write_lock:
                acquired.set()
                assert release.wait(5)
        else:
            conn = sqlite3.connect(n.home / 'state.db', timeout=0)
            try:
                conn.execute('BEGIN IMMEDIATE')
                acquired.set()
                assert release.wait(5)
            finally:
                conn.rollback()
                conn.close()
    worker = threading.Thread(target=hold)
    worker.start()
    try:
        assert acquired.wait(5)
        return acknowledge(*args)  # must finish while the native fence is held
    finally:
        release.set()
        worker.join(5)
        assert not worker.is_alive()


@pytest.mark.parametrize('failure', ['source', 'working_grant', 'egress', 'claim', 'busy'])
def test_recipient_specific_send_gate_revalidates_before_http(native, monkeypatch, failure):
    n = native
    sources = seed(n)
    admit = n.facade.admit_dispatch
    observed = []
    def changed(request):
        # Original actual snapshot and one-use capability; no injection or setter.
        assert request['state']['facts']['premise'] == sources[0][1]
        observed.append(True)
        if failure == 'source':
            n.engine._store._conn.execute("DELETE FROM messages WHERE store_id=?", (sources[0][0],))
            n.engine._store._conn.commit()
        elif failure == 'working_grant':
            n.ctx.supervision._literal_source_registration.working_recipients = frozenset()
        elif failure == 'egress':
            n.facade._registration.egress_policy = {}
        elif failure == 'claim':
            n.rt.artifacts[str(n.claim_file)] = 'changed'
        else:
            return busy_ack(n, lambda *_: admit(request), (), 'source_busy')
        return admit(request)
    monkeypatch.setattr(n.facade, 'admit_dispatch', changed)
    encoded = recall(n)
    assert observed and not n.calls and not consumed(n)
    assert FIELD not in json.loads(encoded) and not n.rt.owner_selections


def test_working_reader_uses_registered_connection_not_replaced_path(native, tmp_path):
    import os
    import sqlite3
    n = native
    first, _ = seed(n)
    store = n.engine._store
    source = n.ctx.supervision._literal_source_registration
    assert source.provider.working_literal_source(n.engine, first[2]) is not None
    backup_path = tmp_path / 'old-backup.db'
    backup = sqlite3.connect(backup_path)
    store._conn.backup(backup)
    backup.close()
    store._conn.execute('DELETE FROM messages WHERE store_id=?', (first[0],))
    store._conn.commit()
    os.replace(backup_path, store.db_path)
    assert store.get(first[0]) is None
    assert source.provider.working_literal_source(n.engine, first[2]) is None
    assert not n.calls
