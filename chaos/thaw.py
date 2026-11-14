"""Short-lived safety guardian: thaw even if the attacking process is killed."""
import sys
import time
from .faults import _frozen, _thaw

if __name__ == "__main__":
    pid, started, seconds = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])
    deadline = time.monotonic() + seconds
    try:
        while time.monotonic() < deadline:
            if not _frozen().find_one({"_id": pid, "started": started}):
                break  # reset already thawed it
            time.sleep(0.2)
    finally:
        _thaw(pid, started)
        _frozen().delete_one({"_id": pid, "started": started})
