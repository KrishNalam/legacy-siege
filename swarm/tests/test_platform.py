"""Platform switch, untrusted tool safety, and real worker/Guard execution contract."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from common.db import now
from swarm import lessons, platform, policy, supervisor
from swarm.tests.conftest import reset_runtime, start_swarm, run_until_resolved, wait_for


@pytest.fixture
def engineer(db, monkeypatch):
    monkeypatch.setenv('PLATFORM_ENGINEERS', '1')
    for name in ('tools', 'tool_runs', 'lessons', 'platform_signals'):
        db[name].delete_many({})
    lines = []
    agent = SimpleNamespace(systems=SimpleNamespace(version=lambda _: 1),
                            think=lambda text, **kw: lines.append(text), id='platform-1')
    yield agent, lines
    for name in ('tools', 'tool_runs', 'lessons', 'platform_signals'):
        db[name].delete_many({})


def seed():
    step = {'step_id': 'INC-X-s1', 'system': 'mainframe', 'op': 'quarantine_record',
            'params': {'record_id': 'OLD'}, 'response': {'accepted': True, 'applied': True}}
    for i in range(2):
        lessons.record_failure('INC-X', step, f'a{i}', 'ACK ≠ APPLIED: still poisoned', 'guard-1', 1, 'batch_abort')


def test_switch_and_roster(db, monkeypatch):
    monkeypatch.delenv('PLATFORM_ENGINEERS', raising=False)
    monkeypatch.delenv('SWARM_ROSTER', raising=False)
    assert not platform.enabled()
    assert not any(r == 'platform' for r, _ in supervisor.roster())
    assert platform.lookup('batch_abort', {}, lambda _: 1) is None
    monkeypatch.setenv('PLATFORM_ENGINEERS', '1')
    for dynamic in ('0', '1'):
        monkeypatch.setenv('SWARM_DYNAMIC', dynamic)
        assert supervisor.roster().count(('platform', 'platform-1')) == 1


def test_tool_rebind_safety_and_retirement(engineer, db):
    agent, lines = engineer
    seed()
    platform.build(agent)
    tool, ops = platform.lookup('batch_abort', {'poison_records': ['NEW', 'NEW2']}, lambda _: 1)
    assert tool['source'] == 'policy'
    assert ops == policy.fix_plan('batch_abort', {'poison_records': ['NEW', 'NEW2']})
    assert platform.lookup('batch_abort', {}, lambda _: 2) is None
    for change in ('params', 'op', 'destructive', 'order'):
        bad = deepcopy(tool['ops'])
        if change == 'params': bad[0]['params'] = {'record_id': 'OLD'}
        if change == 'op': bad[0]['op'] = 'exec'
        if change == 'destructive': bad[1]['destructive'] = False
        if change == 'order': bad.reverse()
        db.tools.update_one({'_id': tool['_id']}, {'$set': {'ops': bad}})
        assert platform.lookup('batch_abort', {}, lambda _: 1) is None
    db.tools.update_one({'_id': tool['_id']}, {'$set': {'ops': tool['ops']}})
    step = {'tool_id': tool['_id'], 'system': 'mainframe'}
    platform.used(agent, 'INC-Y', step)
    platform.used(agent, 'INC-Y', step)
    assert db.tools.find_one()['uses'] == 1
    platform.judged(agent, 'INC-Y', step, False)
    platform.judged(agent, 'INC-Y', step, False)
    assert db.tools.find_one()['failures'] == 1
    assert db.tools.find_one()['status'] == 'retired'
    assert platform.lookup('batch_abort', {}, lambda _: 1) is None
    platform.build(agent)
    assert db.tools.count_documents({}) == 1


def test_claude_validated_and_fallback(engineer, db, monkeypatch):
    agent, _ = engineer
    seed()
    for safe in (False, True):
        db.tools.delete_many({})
        ops = platform.template('batch_abort')
        if not safe: ops[0]['op'] = 'shell'
        monkeypatch.setattr(platform, 'guarded_json', lambda *a, **kw: (
            {'name': 'Claude tool', 'why': 'repeated failures', 'ops': ops}, {'outcome': 'ok'}))
        platform.build(agent)
        assert db.tools.find_one()['source'] == ('claude' if safe else 'policy')
        assert platform.valid(db.tools.find_one()['ops'], 'batch_abort')


@pytest.mark.parametrize("dynamic", ["0", "1"])
def test_live_engineer_build_worker_use_guard_success(engineer, db, fake, dynamic):
    reset_runtime()
    fake.post('/_admin/reset')
    s = start_swarm({'PLATFORM_ENGINEERS': '1', 'SWARM_DYNAMIC': dynamic},
                    log_name=f'platform_{dynamic}', agents=8 if dynamic == '1' else 13)
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
        assert all(st['status'] == 'verified' and st.get('judged_by') == 'guard-1' for st in incident['steps'])
        assert db.thoughts.find_one({'text': {'$regex': 'Platform engineer built tool'}})
        assert db.thoughts.find_one({'text': {'$regex': 'Worker used tool'}})
        fake.post('/_admin/reset')
        fake.post('/mainframe/_chaos/lying_success', json={'params': {'count': 1}})
        fake.post('/mainframe/_chaos/batch_abort', json={'params': {}})
        run_until_resolved(3, timeout=90)
        retired = wait_for(lambda: db.tools.find_one({'status': 'retired'}), 10)
        assert retired['uses'] == 2 and retired['failures'] == 1 and retired['success_rate'] == 0.5
        assert db.thoughts.find_one({'text': {'$regex': 'retired after failed Guard'}})
    finally:
        s.stop()
        reset_runtime()


def test_repeated_slow_verified_steps_build_tool(engineer, db):
    from datetime import timedelta
    agent, _ = engineer
    db.incidents.insert_one({'_id': 'INC-SLOW', 'system': 'mainframe', 'status': 'resolved',
                             'opened_at': now(), 'fault_class': 'batch_abort'})
    step = {'system': 'mainframe', 'op': 'restart_batch', 'sent_at': now() - timedelta(seconds=11)}
    platform.judged(agent, 'INC-SLOW', {**step, 'action_id': 'slow1'}, True)
    platform.judged(agent, 'INC-SLOW', {**step, 'action_id': 'slow1'}, True)
    platform.build(agent)
    assert db.tools.count_documents({}) == 0  # one observation, counted once
    platform.judged(agent, 'INC-SLOW', {**step, 'action_id': 'slow2'}, True)
    platform.build(agent)
    assert db.tools.find_one()['built_from'][0]['count'] == 2
    db.incidents.delete_one({'_id': 'INC-SLOW'})
