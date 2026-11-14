"""setup_db's reset/ensure use their own long-timeout client; the global client keeps its 15 s cap."""
from common import db
from scripts import setup_db


def test_reset_client_has_long_socket_timeout_and_global_is_unchanged():
    c = setup_db._setup_client()
    assert c is not db.client()
    assert c.options.pool_options.socket_timeout == setup_db.SETUP_SOCKET_TIMEOUT_MS / 1000 >= 60
    assert db.client().options.pool_options.socket_timeout == 15
