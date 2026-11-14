"""Red-fleet recovery must outlast lies and diagnose a different fault after learning."""
from swarm.tests.conftest import reset_runtime, run_until_resolved, start_swarm


def test_recovery_switch(monkeypatch):
    from swarm import recovery
    monkeypatch.delenv('RED_FLEET', raising=False)
    monkeypatch.delenv('SWARM_RECOVERY', raising=False)
    assert not recovery.enabled()
    monkeypatch.setenv('RED_FLEET', '1')
    assert recovery.enabled()
    monkeypatch.setenv('SWARM_RECOVERY', '0')
    assert not recovery.enabled()


def test_lies_slow_apply_then_disk_fill(db, fake):
    from common.db import col
    reset_runtime()
    fake.post('/_admin/reset')
    hive = start_swarm({'SWARM_RECOVERY': '1', 'RECOVERY_BACKOFF_S': '0.1'}, log_name='recovery')
    try:
        fake.post('/mainframe/_chaos/lying_success', json={'params': {'count': 5}})
        fake.post('/mainframe/_chaos/slow_apply', json={'params': {'count': 1}})
        fake.post('/mainframe/_chaos/batch_abort', json={'params': {}})
        first = run_until_resolved(1)
        assert first['recovery_rounds'] >= 1
        assert len(first['steps'][0]['attempts']) == 6
        assert col('thoughts').find_one({'incident': first['_id'], 'text': {'$regex': 'Recovery 1/'}})
        fake.post('/unix/_chaos/disk_fill', json={'params': {}})
        second = run_until_resolved(2)
        assert second['_id'] != first['_id']
        assert second['fault_class'] == 'disk_fill'
        assert all(s['status'] == 'verified' for s in second['steps'])
        assert 'purge_spool' in [s['op'] for s in second['steps']]
    finally:
        hive.stop()
