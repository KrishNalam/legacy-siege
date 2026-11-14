"""One standing platform engineer, in the supervisor for both fixed and dynamic swarms."""
from .. import platform
from ..harness import main_loop


def run(agent):
    main_loop(agent, lambda: platform.build(agent), 1.0)
