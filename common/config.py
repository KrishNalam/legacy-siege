"""All configuration in one place. Values come from env (or a .env file at repo root)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # optional .env support
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except Exception:  # pragma: no cover
    pass


def env_float(name: str, default: float) -> float:
    """Float from the environment, falling back to ``default`` when unset or malformed."""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


_f = env_float

MONGODB_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27027/?directConnection=true")
WORLD_DB = os.environ.get("WORLD_DB", "world")
SWARM_DB = os.environ.get("SWARM_DB", "swarm")

SYSTEMS_PORT = int(os.environ.get("SYSTEMS_PORT", 8701))
WARROOM_PORT = int(os.environ.get("WARROOM_PORT", 8700))
SYSTEMS_URL = os.environ.get("SYSTEMS_URL", f"http://127.0.0.1:{SYSTEMS_PORT}")
PUBLIC_HOST = os.environ.get("PUBLIC_HOST", "100.105.248.23")

HEARTBEAT_S = _f("HEARTBEAT_S", 3)
LEASE_S = _f("LEASE_S", 10)
PACE = _f("PACE", 1.0)  # multiplier on deliberate "thinking" delays; 0 in tests
SLOW_APPLY_S = _f("SLOW_APPLY_S", 20)
PARTITION_S = _f("PARTITION_S", 15)
NURSE_RESPAWN_S = _f("NURSE_RESPAWN_S", 15)
RECALL_THRESHOLD = _f("RECALL_THRESHOLD", 0.70)
PROVE_WINDOW_S = _f("PROVE_WINDOW_S", 30)  # Gardener prove: replay the fault, watch this long for incidents

# Demo autoplay (war room, opt-in): nights advance by themselves, approvals after the first auto-approve
DEMO_AUTOPLAY = os.environ.get("DEMO_AUTOPLAY", "").strip().lower() in ("1", "true", "yes", "on")
AUTOPLAY_GAP_S = _f("AUTOPLAY_GAP_S", 4)        # pause after a night goes green before the next one starts
AUTOPLAY_APPROVE_S = _f("AUTOPLAY_APPROVE_S", 3)  # a later approval stays on screen this long, then auto-approves

SYSTEM_IDS = ["mainframe", "unix", "portal"]  # + "console" if stretch is built
CHAIN = {"portal": "unix", "unix": "mainframe", "mainframe": None, "console": "portal"}

EMBED_DIM = int(os.environ.get("EMBED_DIM", 1024))
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY") or None
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-4")

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or None
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-haiku-5-5")
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")  # see common.llm.PROVIDERS
LLM_TIMEOUT_S = _f("LLM_TIMEOUT_S", 8)
EMBED_TIMEOUT_S = _f("EMBED_TIMEOUT_S", 4)  # per Voyage call; on timeout/error we fall back to local-hash
EMBED_COOLDOWN_S = _f("EMBED_COOLDOWN_S", 30)  # after a Voyage failure, skip Voyage this long (no repeated stalls)
# Claude second opinion per incident (swarm/second_opinion.py). Off by default; needs ANTHROPIC_API_KEY too.
AI_SECOND_OPINION = os.environ.get("AI_SECOND_OPINION", "").strip().lower() in ("1", "true", "yes", "on")
# Claude reasons at plan time and its plan RUNS when it passes every guardrail (else the rules plan runs). Off by default.
AI_DRIVES = os.environ.get("AI_DRIVES", "").strip().lower() in ("1", "true", "yes", "on")
AI_DRIVE_TIMEOUT_S = _f("AI_DRIVE_TIMEOUT_S", 6.0)  # Haiku answers in ~3-4.5 s under load
# MongoDB spine extras (W25): change-stream pre-images + lease chain-of-custody trail (swarm/custody.py),
# $jsonSchema validators on tasks/approvals/upgrade_requests/playbooks, war-room spine analytics. Off by default.
MONGO_SPINE = os.environ.get("MONGO_SPINE", "").strip().lower() in ("1", "true", "yes", "on")


def pace(seconds: float) -> float:
    """Scaled demo delay."""
    return max(0.0, seconds * PACE)
