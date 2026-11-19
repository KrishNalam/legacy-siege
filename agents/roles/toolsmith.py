"""Toolsmith engineer: a Monitor-type agent (role "monitor", subtype "toolsmith", id toolsmith-N), run from
roles/monitor.run. One standing in the supervisor roster for both fixed and dynamic swarms."""
from .. import toolsmith
from ..harness import main_loop


def run(agent):
    from common.db import col
    col('agents').update_one({'_id': agent.id}, {'$set': {
        'subtype': 'toolsmith', 'capability': 'toolsmith_engineering'}})
    agent.set(phase='watching_patterns')
    main_loop(agent, lambda: toolsmith.build(agent), 1.0)
