"""Replanning must preserve a Guard verdict committed after the scout read."""
import pytest


@pytest.mark.parametrize('status', ['sent', 'verified', 'failed'])
def test_replan_preserves_concurrent_guard_fields(status):
    from common.db import client
    from swarm.roles.scout import _replan_update
    db = client()['swarm_test_w31_replan']
    try:
        steps = db.incidents
        steps.insert_one({'_id': 'I', 'steps': [{'step_id': 's1', 'status': 'planned'}]})
        # Scout has already read planned; Guard now commits its newer state.
        verdict = {'step_id': 's1', 'status': status, 'evidence': 'read-back',
                   'attempts': [{'action_id': 'a1', 'status': status}]}
        steps.update_one({'_id': 'I'}, {'$set': {'steps.0': verdict}})
        replacement = {'step_id': 's2', 'status': 'planned', 'params': {'literal': '$value'}}
        steps.update_one({'_id': 'I'}, _replan_update([replacement]))
        actual = steps.find_one({'_id': 'I'})['steps']
        expected = dict(verdict)
        if status == 'failed':
            expected['superseded_by_replan'] = True
        assert actual == [expected, replacement]
    finally:
        client().drop_database(db.name)
