"""Short-lived safety guardian: thaw even if the attacking process is killed."""
import sys
import time
from .faults import _frozen, _thaw

if __name__ == "__main__":
    pid, started, seconds = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
    try:
        # The safety timer must not wait on MongoDB while a bee is SIGSTOPped.
        # Reset can thaw sooner; the attacker's cleanup reaps this guardian.
        time.sleep(seconds)
    finally:
        _thaw(pid, started)
        _frozen().delete_one({"_id": pid, "started": started})
