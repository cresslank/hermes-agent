"""F09 authorization and F04 reuse must compose in the real dispatcher."""
import json
import pytest

from tests.agent.test_owned_delegation_planning import base_rig, rig, native, factory
from tests.agent.test_supervision_read_supplier import requests, read
from tests.agent.test_supervision_efficiency import feature_calls


@pytest.mark.parametrize('relation', ['within_scope', 'outside_scope'])
def test_authorization_then_reuse_are_distinct_consumable_decisions(factory, monkeypatch, relation):
    from agent.supervision_types import project
    p = factory()
    rows = requests(p)
    p.commit(rows)
    first, original = read(p, rows[0])
    registration = p.native.bridge.native._registration
    section = p.native.rig.config['supervision']['plugins']['fixture-supervisor']
    section['grants'].append('authorize_action')
    section['egress_policy']['fields'].update({
        name: 'synthetic' for name in ('accepted_scope', 'scope_complete', 'action')})
    config_path = p.native.rig.home / 'config.yaml'
    persisted = json.loads(config_path.read_text())
    persisted['supervision']['plugins']['fixture-supervisor']['grants'] = section['grants']
    persisted['supervision']['plugins']['fixture-supervisor']['egress_policy'] = section['egress_policy']
    config_path.write_text(json.dumps(persisted))
    from hermes_cli.config import load_config_readonly
    load_config_readonly()
    registration.grants = frozenset((*registration.grants, 'authorize_action'))
    registration.data_policy = frozenset((*registration.data_policy, 'project_excerpt', 'task_text'))
    registration.egress_policy = project(section['egress_policy'])
    p.native.choice['F09'] = relation
    evaluations = []
    monkeypatch.setattr('agent.tool_executor._run_with_activity_heartbeat',
                        lambda agent, name, run: evaluations.append(name) or run())
    p.a.client.reset_mock()
    _, actual = p.call('read_file', rows[1]['operation']['arguments'], settle=False)
    assert len(feature_calls(p.native, 'F09')) == 1
    assert evaluations == [], (p.native.bridge.supervisor.inspect(), p.rt.receipts, [list(b['questions']) for b in p.native.calls], p.owner.current())
    assert not p.a.client.mock_calls
    envelope = json.loads(actual)
    assert envelope['executed'] is False
    if relation == 'within_scope':
        assert len(feature_calls(p.native, 'F04')) == 1
        assert envelope['reused_from'] == 'tool:' + first
        assert envelope['result'] == original
        reasons = {r.reason for r in p.rt.receipts.values() if r.status == 'applied'}
        assert {'action_authorized', 'existing_result_reused'} <= reasons
    else:
        assert not feature_calls(p.native, 'F04')
        assert 'reused_from' not in envelope
        assert 'outside' in envelope['error']
