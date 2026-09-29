"""PROBE. Delete."""
import pytest
from tests.test_api_routes_post import env  # noqa: F401

OUT = '/tmp/probe_cursor.txt'


@pytest.mark.parametrize('cursor', ['abc', '>s:foo~i:42', '', '1.5', '2', None])
def test_probe(env, cursor):
    qs = {} if cursor is None else {'page_cursor': cursor}
    outcome = 'ok'
    try:
        r = env.client.get('/api/alpha/post/list', query_string=qs)
        status = r.status_code
    except Exception as e:
        status = '-'
        outcome = f'{type(e).__name__}: {str(e)[:56]}'
    with open(OUT, 'a') as fh:
        fh.write(f'page_cursor={cursor!r:14} status={status} {outcome}\n')


def test_what_list2_hands_out(env):
    r = env.client.get('/api/alpha/post/list2')
    with open(OUT, 'a') as fh:
        fh.write(f'list2 next_page={r.get_json().get("next_page")!r}\n')
