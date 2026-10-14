"""PieFedNNTPServer (app/nntp/server.py) refuses malformed client input with the NNTP errors its handler answers.

The connection handler catches NNTPAuthenticationError around auth_user and nothing else, so any other exception
out of it drops the connection without a reply.
"""

from types import SimpleNamespace

import pytest

from app.nntp.nntpserver import NNTPAuthenticationError
from app.nntp.server import PieFedNNTPServer


@pytest.mark.parametrize('user', ['a\x00b', '\x00'])
def test_a_user_name_holding_nul_is_refused_as_a_failed_login(app, user):
    """auth_user put the AUTHINFO USER value in a query, and psycopg2 raises ValueError for a NUL in a string
    literal, before a login, from anyone who can reach the port."""
    with pytest.raises(NNTPAuthenticationError):
        PieFedNNTPServer.auth_user(SimpleNamespace(_app=app), user, 'secret')
