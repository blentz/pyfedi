"""Asking the hashing endpoint what an image is.

Sub-project 122 -- `retrieve_image_hash` in `app/utils.py`. It asks the
configured `IMAGE_HASHING_ENDPOINT` for a PDQ hash of an image url, and the answer
feeds `hash_matches_blocked_image`, which is how an instance refuses an image it
has blocked.

Every value it read came out of that endpoint's response and none of it was
checked, so four answers an endpoint can give with a 200 raised instead of
answering: a `quality` that is a string or null (`TypeError`), a body that is not
JSON (`JSONDecodeError`), and a JSON list (`AttributeError`). None of them is an
`httpx.HTTPError`, so none was caught -- and this function is called from
`Post.new`, where the exception loses the whole federated post, and from the post
and admin routes, where it is a 500 (D1336).

A hash this instance cannot obtain is no hash, which is what every caller already
handles: `app/models.py:2210` and `app/post/routes.py:2467` both test the result
before using it.
"""
import httpx
import pytest
import respx
from flask import current_app, g

from app import db
from app.models import Site
from app.utils import retrieve_image_hash

ENDPOINT = 'https://hash.example/hash'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT', ENDPOINT)
    # The real one sleeps between 429 retries; two of them would add up to six
    # seconds of a worker's time inside the inbox path.
    import app.utils as utils
    monkeypatch.setattr(utils, 'sleep', lambda seconds: None)
    return SimpleNamespace(app=app)


def ask(url_suffix, **response):
    """One lookup. The function is memoized, so every case needs its own url."""
    url = f'https://images.example/{url_suffix}.png'
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith=ENDPOINT).mock(
            return_value=httpx.Response(**response))
        return retrieve_image_hash(url), route


class TestAnAnswerItCanUse:
    def test_a_hash_of_good_quality(self, env):
        answer, _route = ask('good', status_code=200,
                             json={'quality': 90, 'pdq_hash_binary': '0101'})
        assert answer == '0101'

    def test_quality_exactly_at_the_threshold(self, env):
        answer, _route = ask('exactly', status_code=200,
                             json={'quality': 70, 'pdq_hash_binary': '1100'})
        assert answer == '1100'

    def test_a_float_quality(self, env):
        answer, _route = ask('float', status_code=200,
                             json={'quality': 70.5, 'pdq_hash_binary': '1'})
        assert answer == '1'

    def test_the_image_url_is_passed_to_the_endpoint(self, env):
        _answer, route = ask('passed', status_code=200,
                             json={'quality': 90, 'pdq_hash_binary': '0101'})
        assert 'images.example%2Fpassed.png' in str(route.calls[0].request.url) \
            or 'images.example/passed.png' in str(route.calls[0].request.url)


class TestAnAnswerItCannotUse:
    def test_quality_below_the_threshold(self, env):
        answer, _route = ask('low', status_code=200,
                             json={'quality': 10, 'pdq_hash_binary': '0101'})
        assert answer is None

    def test_no_quality_at_all(self, env):
        """The key is absent, so `.get('quality', 0)` answers 0."""
        answer, _route = ask('noquality', status_code=200,
                             json={'pdq_hash_binary': '0101'})
        assert answer is None

    def test_no_hash_in_a_good_answer(self, env):
        """`''` rather than None, which every caller treats the same way -- and
        `hash_matches_blocked_image('')` is False because its pattern needs at
        least one digit."""
        from app.utils import hash_matches_blocked_image

        answer, _route = ask('nohash', status_code=200, json={'quality': 90})
        assert answer == ''
        assert hash_matches_blocked_image('') is False

    @pytest.mark.parametrize('quality', ['high', None, [], {}, True, False])
    def test_a_quality_that_is_not_a_number(self, env, quality):
        """D1336. A string or null was `TypeError: '>=' not supported between
        instances of 'str' and 'int'`.

        The two booleans reach None by a different route from the rest: `True` is
        an `int` in Python, so the type check lets it through and `True >= 70`
        refuses it. That is why there is no explicit `isinstance(quality, bool)`
        clause in the function -- a mutant removing one survived, because it
        cannot change any answer."""
        answer, _route = ask(f'quality_{quality!r}', status_code=200,
                             json={'quality': quality, 'pdq_hash_binary': '0101'})
        assert answer is None

    def test_a_hash_that_is_not_a_string(self, env):
        answer, _route = ask('hashnum', status_code=200,
                             json={'quality': 90, 'pdq_hash_binary': 1010})
        assert answer is None

    def test_a_body_that_is_not_json(self, env):
        """D1336. `JSONDecodeError` is not an `httpx.HTTPError`."""
        answer, _route = ask('nojson', status_code=200, text='<html>nope</html>')
        assert answer is None

    def test_an_empty_body(self, env):
        answer, _route = ask('empty', status_code=200, text='')
        assert answer is None

    @pytest.mark.parametrize('payload', [[1, 2], 'a string', 5, None, True])
    def test_json_that_is_not_an_object(self, env, payload):
        """D1336. A list was `AttributeError: 'list' object has no attribute
        'get'`."""
        answer, _route = ask(f'shape_{payload!r}', status_code=200, json=payload)
        assert answer is None

    @pytest.mark.parametrize('status', [400, 403, 404, 500, 502, 503])
    def test_a_status_that_is_not_200(self, env, status):
        answer, _route = ask(f'status{status}', status_code=status, text='no')
        assert answer is None


class TestWhenTheEndpointIsUnreachable:
    def _failing(self, url_suffix, error):
        url = f'https://images.example/{url_suffix}.png'
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=ENDPOINT).mock(side_effect=error)
            return retrieve_image_hash(url)

    def test_a_read_error(self, env):
        """`httpx.ReadError` had a clause of its own, after the `HTTPError` one
        it is a subclass of, so that clause had never run. It is gone, and this
        asserts the behaviour did not move with it."""
        assert self._failing('readerror', httpx.ReadError('boom')) is None

    def test_a_connect_error(self, env):
        assert self._failing('connect', httpx.ConnectError('refused')) is None

    def test_a_timeout(self, env):
        assert self._failing('timeout', httpx.ReadTimeout('too slow')) is None

    def test_the_read_error_clause_is_gone_from_this_function(self, env):
        """Scoped to `retrieve_image_hash` with `inspect.getsource`, not to the
        file: `get_request` further up has a `ReadError` clause of its own, and
        there it is load-bearing -- it retries with a longer timeout before any
        HTTPError conversion. Asserting over the whole module would forbid that
        one too."""
        import inspect

        from app.utils import retrieve_image_hash as target

        source = inspect.getsource(target.uncached if hasattr(target, 'uncached')
                                   else target)
        assert 'except httpx.ReadError' not in source
        assert 'except httpx.HTTPError' in source

    def test_read_error_really_is_an_http_error(self, env):
        """Why that clause was dead, asserted rather than asserted-about."""
        assert issubclass(httpx.ReadError, httpx.HTTPError)


class TestWhenTheEndpointIsBusy:
    def test_a_429_is_retried_and_then_given_up_on(self, env):
        url = 'https://images.example/busy.png'
        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(url__startswith=ENDPOINT).mock(
                return_value=httpx.Response(429, text='slow down'))
            answer = retrieve_image_hash(url)
        assert answer is None
        assert len(route.calls) == 3, 'one attempt plus two retries'

    def test_a_429_followed_by_an_answer(self, env):
        url = 'https://images.example/busythenfine.png'
        answers = [httpx.Response(429, text='slow down'),
                   httpx.Response(200, json={'quality': 90,
                                             'pdq_hash_binary': '0101'})]
        with respx.mock(assert_all_called=False) as mock:
            route = mock.get(url__startswith=ENDPOINT).mock(
                side_effect=answers)
            assert retrieve_image_hash(url) == '0101'
        assert len(route.calls) == 2

    def test_a_429_whose_retry_returns_something_unreadable(self, env):
        """The retry goes through the same guards, not around them."""
        url = 'https://images.example/busythenjunk.png'
        answers = [httpx.Response(429, text='slow down'),
                   httpx.Response(200, text='<html>nope</html>')]
        with respx.mock(assert_all_called=False) as mock:
            mock.get(url__startswith=ENDPOINT).mock(side_effect=answers)
            assert retrieve_image_hash(url) is None
