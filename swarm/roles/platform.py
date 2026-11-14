"""One standing platform engineer, in the supervisor for both fixed and dynamic swarms."""
from .. import platform
from ..harness import main_loop


def run(agent):
    from common.db import col
    col('agents').update_one({'_id': agent.id}, {'$set': {
        'agent_type': 'sentinel', 'capability': 'platform_engineering'}})
    agent.set(phase='watching_patterns')
    main_loop(agent, lambda: platform.build(agent), 1.0)
