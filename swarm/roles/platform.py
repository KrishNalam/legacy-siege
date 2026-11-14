"""Platform engineer: a Sentinel-type agent (role "sentinel", subtype "platform", id platform-N), run from
roles/sentinel.run. One standing in the supervisor roster for both fixed and dynamic swarms."""
from .. import platform
from ..harness import main_loop


def run(agent):
    from common.db import col
    col('agents').update_one({'_id': agent.id}, {'$set': {
        'subtype': 'platform', 'capability': 'platform_engineering'}})
    agent.set(phase='watching_patterns')
    main_loop(agent, lambda: platform.build(agent), 1.0)
