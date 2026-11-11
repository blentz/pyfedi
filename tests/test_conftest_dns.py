"""The suite's hosts are under the reserved names .test, .example and .invalid (RFC 2606, RFC 6761 §6.2, §6.5),
which no resolver may answer. The suite answers them itself, at once, as a resolver does: a code path that resolves
one (is_invalid_get_request_uri's private-address check) must not wait on whatever resolver the machine running
the tests has, which on the remote test host took 20s to say no."""
import socket
import time

import pytest


@pytest.mark.parametrize('host', ['remote.test', 'shop.example', 'nowhere.invalid', 'UPPER.TEST.', b'bytes.test'])
def test_a_reserved_name_is_not_known_at_once(host):
    started = time.monotonic()
    with pytest.raises(socket.gaierror) as refused:
        socket.getaddrinfo(host, 443)
    assert refused.value.errno == socket.EAI_NONAME
    assert time.monotonic() - started < 0.5


def test_other_names_go_to_the_resolver():
    assert socket.getaddrinfo('127.0.0.1', 80)[0][4][0] == '127.0.0.1'
    assert socket.getaddrinfo('localhost', 80)
