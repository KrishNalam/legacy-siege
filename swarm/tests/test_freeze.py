"""A live but SIGSTOPped worker must lose its task to a fenced Nurse."""
import os
import signal

from swarm.tests.conftest import reset_runtime, run_until_resolved, start_swarm, wait_for


def test_frozen_worker_nurse_takeover_and_resume(db, fake):
    from common.db import col
    reset_runtime()
    fake.post('/_admin/reset')
    hive = start_swarm({'STEP_HOLD_S': '1.5', 'SWARM_LEASE_DEADLINE': '1'}, log_name='freeze')
    victim = None
    try:
        fake.post('/mainframe/_chaos/batch_abort', json={'params': {}})
        victim = wait_for(lambda: col('agents').find_one({'role': 'worker', 'phase': 'step_sent'}), 60, .05)
        os.kill(victim['pid'], signal.SIGSTOP)
        inc = run_until_resolved(1)
        assert any(h['from'] == victim['_id'] and h['to'].startswith('nurse-') for h in inc['handoffs'])
        task = col('tasks').find_one({'_id': victim['task']})
        assert task['generation'] >= 2
        os.kill(victim['pid'], signal.SIGCONT)
        wait_for(lambda: col('agents').find_one({'_id': victim['_id'], 'status': 'idle'}), 15)
        assert all(len(s['attempts']) == 1 for s in inc['steps'])
        assert set(fake.get('/_fake/receipts').json()['applied'].values()) == {1}
    finally:
        if victim:
            try:
                os.kill(victim['pid'], signal.SIGCONT)
            except ProcessLookupError:
                pass
        hive.stop()
