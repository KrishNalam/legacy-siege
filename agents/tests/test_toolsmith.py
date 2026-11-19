"""Toolsmith switch, untrusted tool safety, and real operator/Verifier execution contract."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from common.db import now
from agents import lessons, toolsmith, policy, supervisor
from agents.tests.conftest import reset_runtime, start_swarm, run_until_resolved, wait_for


@pytest.fixture
def engineer(db, monkeypatch):
    monkeypatch.setenv('TOOLSMITH_ENGINEERS', '1')
    for name in ('tools', 'tool_runs', 'lessons', 'platform_signals', 'gardener_requests'):
        db[name].delete_many({})
    lines = []
    agent = SimpleNamespace(systems=SimpleNamespace(version=lambda _: 1),
                            think=lambda text, **kw: lines.append(text), id='toolsmith-1')
    yield agent, lines
    for name in ('tools', 'tool_runs', 'lessons', 'platform_signals', 'gardener_requests'):
        db[name].delete_many({})


def seed():
    step = {'step_id': 'INC-X-s1', 'system': 'mainframe', 'op': 'quarantine_record',
            'params': {'record_id': 'OLD'}, 'response': {'accepted': True, 'applied': True}}
    for i in range(2):
        lessons.record_failure('INC-X', step, f'a{i}', 'ACK ≠ APPLIED: still poisoned', 'verifier-1', 1, 'batch_abort')


def test_switch_and_roster(db, monkeypatch):
    monkeypatch.delenv('TOOLSMITH_ENGINEERS', raising=False)
    monkeypatch.delenv('AGENTS_ROSTER', raising=False)
    assert not toolsmith.enabled()
    assert not any(i.startswith('toolsmith-') for _, i in supervisor.roster())
    assert toolsmith.lookup('batch_abort', {}, lambda _: 1) is None
    monkeypatch.setenv('TOOLSMITH_ENGINEERS', '1')
    for dynamic in ('0', '1'):
        monkeypatch.setenv('AGENTS_AUTOSCALE', dynamic)
        assert supervisor.roster().count(('monitor', 'toolsmith-1')) == 1


def test_tool_rebind_safety_and_retirement(engineer, db):
    agent, lines = engineer
    seed()
    toolsmith.build(agent)
    tool, ops = toolsmith.lookup('batch_abort', {'poison_records': ['NEW', 'NEW2']}, lambda _: 1)
    assert tool['source'] == 'policy'
    assert ops == policy.fix_plan('batch_abort', {'poison_records': ['NEW', 'NEW2']})
    assert toolsmith.lookup('batch_abort', {}, lambda _: 2) is None
    for change in ('params', 'op', 'destructive', 'order'):
        bad = deepcopy(tool['ops'])
        if change == 'params': bad[0]['params'] = {'record_id': 'OLD'}
        if change == 'op': bad[0]['op'] = 'exec'
        if change == 'destructive': bad[1]['destructive'] = False
        if change == 'order': bad.reverse()
        db.tools.update_one({'_id': tool['_id']}, {'$set': {'ops': bad}})
        assert toolsmith.lookup('batch_abort', {}, lambda _: 1) is None
    db.tools.update_one({'_id': tool['_id']}, {'$set': {'ops': tool['ops']}})
    step = {'tool_id': tool['_id'], 'system': 'mainframe'}
    toolsmith.used(agent, 'INC-Y', step)
    toolsmith.used(agent, 'INC-Y', step)
    assert db.tools.find_one()['uses'] == 1
    toolsmith.judged(agent, 'INC-Y', step, False)
    toolsmith.judged(agent, 'INC-Y', step, False)
    assert db.tools.find_one()['failures'] == 1
    assert db.tools.find_one()['status'] == 'retired'
    assert toolsmith.lookup('batch_abort', {}, lambda _: 1) is None
    toolsmith.build(agent)
    assert db.tools.count_documents({}) == 1


def test_claude_validated_and_fallback(engineer, db, monkeypatch):
    agent, _ = engineer
    seed()
    for safe in (False, True):
        db.tools.delete_many({})
        ops = toolsmith.template('batch_abort')
        if not safe: ops[0]['op'] = 'shell'
        monkeypatch.setattr(toolsmith, 'guarded_json', lambda *a, **kw: (
            {'name': 'Claude tool', 'why': 'repeated failures', 'ops': ops}, {'outcome': 'ok'}))
        toolsmith.build(agent)
        assert db.tools.find_one()['source'] == ('claude' if safe else 'policy')
        assert toolsmith.valid(db.tools.find_one()['ops'], 'batch_abort')


@pytest.mark.parametrize("dynamic", ["0", "1"])
def test_live_engineer_build_operator_use_verifier_success(engineer, db, fake, dynamic):
    reset_runtime()
    fake.post('/_admin/reset')
    s = start_swarm({'TOOLSMITH_ENGINEERS': '1', 'AGENTS_AUTOSCALE': dynamic},
                    log_name=f'toolsmith_{dynamic}', agents=8 if dynamic == '1' else 13)
    try:
        fake.post('/mainframe/_chaos/lying_success', json={'params': {'count': 2}})
        fake.post('/mainframe/_chaos/batch_abort', json={'params': {}})
        first = run_until_resolved(1, timeout=90)
        assert len(first['steps'][0]['attempts']) == 3
        wait_for(lambda: db.tools.find_one({'status': 'active'}), 30)
        fake.post('/_admin/reset')
        fake.post('/mainframe/_chaos/batch_abort', json={'params': {}})
        incident = run_until_resolved(2, timeout=90)
        tool = wait_for(lambda: db.tools.find_one({'successes': 1}), 10)
        assert tool['uses'] == 1 and tool['success_rate'] == 1
        request = db.gardener_requests.find_one({'kind': 'tool', 'status': 'open'})
        assert request['payload']['tool_id'] == tool['_id'] and request['from'] == 'toolsmith-1'
        assert db.messages.find_one({'to': 'investigators', 'kind': 'tool', 'body.payload.tool_id': tool['_id']})
        assert db.tasks.find_one({'incident': incident['_id'], 'tool_ids': tool['_id']})
        assert db.agents.find_one({'toolsmith_tools': tool['_id']})
        assert all(st['status'] == 'verified' and st.get('judged_by') == 'verifier-1' for st in incident['steps'])
        assert db.thoughts.find_one({'text': {'$regex': 'Toolsmith engineer built tool'}})
        assert db.thoughts.find_one({'text': {'$regex': 'Operator used tool'}})
        fake.post('/_admin/reset')
        fake.post('/mainframe/_chaos/lying_success', json={'params': {'count': 1}})
        fake.post('/mainframe/_chaos/batch_abort', json={'params': {}})
        run_until_resolved(3, timeout=90)
        retired = wait_for(lambda: db.tools.find_one({'status': 'retired'}), 10)
        assert retired['uses'] == 2 and retired['failures'] == 1 and retired['success_rate'] == 0.5
        assert db.thoughts.find_one({'text': {'$regex': 'retired after failed Verifier'}})
    finally:
        s.stop()
        reset_runtime()


def test_repeated_slow_verified_steps_build_tool(engineer, db):
    from datetime import timedelta
    agent, _ = engineer
    db.incidents.insert_one({'_id': 'INC-SLOW', 'system': 'mainframe', 'status': 'resolved',
                             'opened_at': now(), 'fault_class': 'batch_abort'})
    step = {'system': 'mainframe', 'op': 'restart_batch', 'sent_at': now() - timedelta(seconds=11)}
    toolsmith.judged(agent, 'INC-SLOW', {**step, 'action_id': 'slow1'}, True)
    toolsmith.judged(agent, 'INC-SLOW', {**step, 'action_id': 'slow1'}, True)
    toolsmith.build(agent)
    assert db.tools.count_documents({}) == 0  # one observation, counted once
    toolsmith.judged(agent, 'INC-SLOW', {**step, 'action_id': 'slow2'}, True)
    toolsmith.build(agent)
    assert db.tools.find_one()['built_from'][0]['count'] == 2
    db.incidents.delete_one({'_id': 'INC-SLOW'})


def test_delivery_replays_after_publish_crash_and_revokes(engineer, db):
    from agents import tool_delivery
    agent, _ = engineer
    seed()
    toolsmith.build(agent)
    tool = db.tools.find_one()
    db.messages.delete_many({'kind': 'tool'})
    db.gardener_requests.delete_many({'kind': 'tool'})
    toolsmith.build(agent)  # registry already exists: recover missed delivery
    toolsmith.build(agent)  # no duplicate delivery
    assert db.gardener_requests.count_documents({'kind': 'tool'}) == 1
    db.agents.insert_one({'_id': agent.id, 'role': 'monitor', 'subtype': 'toolsmith', 'status': 'idle'})
    assert tool_delivery.refresh(agent) == [tool['_id']]
    db.tools.update_one({'_id': tool['_id']}, {'$set': {'status': 'retired'}})
    toolsmith.build(agent)
    assert tool_delivery.refresh(agent, 'reporter_bus') == []
    assert db.gardener_requests.count_documents({'kind': 'tool'}) == 2
    assert db.messages.find_one({'kind': 'tool', 'body.payload.status': 'retired'})
    db.agents.delete_one({'_id': agent.id})


def test_lie_plus_slow_step_on_same_fault_builds_tool(engineer, db):
    """Night 1 alone (one Verifier-caught lie + one slow verified restart) qualifies, so night 2 can use the tool."""
    from datetime import timedelta
    agent, _ = engineer
    lessons.record_failure('INC-N1', {'step_id': 'INC-N1-s1', 'system': 'mainframe', 'op': 'quarantine_record',
                                      'params': {'record_id': 'OLD'}, 'response': {'accepted': True, 'applied': True}}, 'a1',
                           'ACK ≠ APPLIED: still poisoned', 'verifier-1', 1, 'batch_abort')
    toolsmith.build(agent)
    assert db.tools.count_documents({}) == 0  # a single observation is not repeated
    db.incidents.insert_one({'_id': 'INC-N1', 'system': 'mainframe', 'status': 'resolved',
                             'opened_at': now(), 'fault_class': 'batch_abort'})
    toolsmith.judged(agent, 'INC-N1', {'system': 'mainframe', 'op': 'restart_batch', 'action_id': 's1',
                                      'sent_at': now() - timedelta(seconds=11)}, True)
    toolsmith.build(agent)
    tool = db.tools.find_one()
    assert tool and sorted(b['count'] for b in tool['built_from']) == [1, 1]
    assert db.gardener_requests.count_documents({'kind': 'tool'}) == 1
    db.incidents.delete_one({'_id': 'INC-N1'})
