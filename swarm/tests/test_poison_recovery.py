"""A poisoned recalled repair fails Guard read-back, is retired, and gets a fresh policy plan."""
from swarm.tests.conftest import reset_runtime, run_until_resolved, start_swarm, wait_for


def test_poisoned_recalled_repair_is_rejected_and_replaced(db, fake):
    from common.db import col
    from swarm import recall
    reset_runtime()
    col('lessons').delete_many({})
    fake.post('/_admin/reset')
    hive = start_swarm({'SWARM_RECOVERY': '1', 'FAILURE_MEMORY': '1', 'RECOVERY_BACKOFF_S': '0.1'},
                       log_name='poison')
    try:
        fake.post('/unix/_chaos/disk_fill', json={'params': {}})
        first = run_until_resolved(1)
        pb = col('playbooks').find_one({'incident': first['_id']})
        # Right symptom/fault label, but a repair directed at the wrong system.
        poisoned = [{'system': 'mainframe', 'op': 'purge_spool', 'params': {'path': '/var/spool/xfer'},
                     'desc': 'Poisoned memory', 'destructive': False}]
        col('playbooks').update_one({'_id': pb['_id']}, {'$set': {'fix': poisoned}})
        wait_for(lambda: recall.search(pb['symptom_text'])[1], 30)
        fake.post('/unix/_chaos/disk_fill', json={'params': {}})
        second = run_until_resolved(2)
        assert second['recall']['used']
        assert second['recall']['rejected'] == pb['_id']
        assert second['memory_replanned']
        assert col('playbooks').find_one({'_id': pb['_id']})['stale']
        assert col('lessons').find_one({'kind': 'misfit', 'playbook': pb['_id']})
        assert col('thoughts').find_one({'incident': second['_id'], 'kind': 'reject',
                                        'text': {'$regex': 'Rejected memory'}})
        assert any(s.get('superseded_by_replan') and s['status'] == 'failed' for s in second['steps'])
        learned = col('playbooks').find_one({'incident': second['_id']})
        assert all(s['system'] != 'mainframe' for s in learned['fix'])
        assert not any(h['_id'] == pb['_id'] for h in recall.search(pb['symptom_text'])[1])
    finally:
        hive.stop()
        col('lessons').delete_many({})


def test_poison_rejection_requires_both_switches(monkeypatch):
    from swarm import recovery
    monkeypatch.setattr('common.db.col', lambda *a: (_ for _ in ()).throw(AssertionError('unexpected DB access')))
    monkeypatch.delenv('RED_FLEET', raising=False)
    monkeypatch.delenv('SWARM_RECOVERY', raising=False)
    monkeypatch.setenv('FAILURE_MEMORY', '1')
    assert recovery.reject_bad_memory('I', 'failure', 'guard-1') is None
    monkeypatch.setenv('SWARM_RECOVERY', '1')
    monkeypatch.setenv('FAILURE_MEMORY', '0')
    assert recovery.reject_bad_memory('I', 'failure', 'guard-1') is None
