"""app/utils.py: the arms the per-topic suites never reached (coverage-floor work).

Each test names the decision the arm makes. Several are guards that fail
closed on something a remote peer controls (the nodeinfo and blocklist
readers, the capped fetcher), so the useful assertion is always "what
happened to the bad input", never "it ran".
"""
import httpx
import pytest

from app import db
from app.models import Tag
from app.utils import (_nodeinfo_json, getmtime, can_create_post, get_request_capped, handle_lemmy_autocomplete,
                       login_required, remote_instance_software, retrieve_defederation_list)
from tests.factories import (feed_ids, make_community, make_instance, make_instance_ban, make_post,
                             make_feed_viewer, make_user)

PEER = 'nodeinfo.example'


def test_get_request_capped_refuses_an_invalid_uri_without_fetching(app):
    """`.local` hosts are refused before any socket is opened (the SSRF guard); the refusal is the same
    `httpx.HTTPError` every other failure of this function is normalised to, so callers need one handler."""
    with pytest.raises(httpx.HTTPError, match='invalid uri'):
        get_request_capped('https://printer.local/feed.xml', 1000)


def test_get_request_capped_normalises_a_transport_level_oddity_to_http_error(app, http_mock):
    """A URL httpx itself rejects (InvalidURL, a ValueError subclass family) must not escape as a different
    exception type from a best-effort fetch."""
    http_mock.get('https://peer.example/feed.xml').mock(side_effect=httpx.InvalidURL('bad url'))

    with pytest.raises(httpx.HTTPError, match='bad url'):
        get_request_capped('https://peer.example/feed.xml', 1000)


def test_a_markdown_link_that_only_looks_like_a_mention_is_left_alone():
    """`[@bob](https://x/u/bob)` has no `@name@host` inside the brackets, so it is an ordinary link the
    author wrote and must not be flattened to bare text."""
    text = 'see [@bob](https://x.example/u/bob) and [!news](https://x.example/c/news)'

    assert handle_lemmy_autocomplete(text) == text


def test_a_lemmy_autocomplete_feed_link_is_flattened_to_the_handle():
    assert handle_lemmy_autocomplete('[~tech@lemmy.world](https://lemmy.world/f/tech)') == '~tech@lemmy.world'


def _view():
    return 'ran'


@pytest.mark.parametrize('method, config', [('OPTIONS', {}), ('GET', {'LOGIN_DISABLED': True})])
def test_login_required_lets_a_preflight_or_a_login_disabled_deployment_through(app, monkeypatch, method, config):
    """A CORS preflight carries no credentials and a LOGIN_DISABLED deployment has turned the check off: neither
    may be bounced to the login page."""
    for key, value in config.items():
        monkeypatch.setitem(app.config, key, value)

    with app.test_request_context('/', method=method):
        assert login_required()(_view)() == 'ran'


def test_login_required_calls_the_view_directly_when_the_app_has_no_ensure_sync(app, monkeypatch):
    """Flask 1.x has no `ensure_sync`; the decorator then calls the view itself rather than failing."""
    monkeypatch.setattr(app, 'ensure_sync', None, raising=False)

    with app.test_request_context('/', method='OPTIONS'):
        assert login_required()(_view)() == 'ran'


def test_can_create_post_refuses_a_banned_instance_even_when_the_memoised_ban_list_is_stale(app, db_session,
                                                                                           monkeypatch):
    """`communities_banned_from` is memoised for a day, so right after an admin bans an instance for a user the
    cached community list can still omit that instance's communities. `banned_instances` is the independent
    backstop that stops the post."""
    make_instance('test.piefed.local', software='piefed')
    remote = make_instance('communityinstance.example')
    make_user(None, 'filler', local=True)
    user = make_user(None, 'staleban', local=True, with_keys=True)
    community = make_community('stalecache')
    community.instance_id = remote.id
    db.session.commit()
    make_instance_ban(user, remote)
    monkeypatch.setattr('app.utils.communities_banned_from', lambda user_id: [])

    assert can_create_post(user, community) is False


def test_a_hashtag_narrows_a_listing_to_posts_carrying_it(app, db_session, redis_double):
    make_instance('tagged.example')
    viewer = make_feed_viewer(None, 'tagviewer', local=True)
    author = make_user(None, 'tagauthor', local=True)
    community = make_community('tagcomm')
    tagged = make_post(community, author, 'https://tagged.example/posts/1')
    plain = make_post(community, author, 'https://tagged.example/posts/2')
    tag = Tag(name='solarstorm', display_as='SolarStorm')
    db.session.add(tag)
    tagged.tags.append(tag)
    db.session.commit()

    ids = feed_ids(app, viewer, [community.id], hashtag='solarstorm')

    assert tagged.id in ids
    assert plain.id not in ids


DEFED_DOMAIN = 'faraway.test'


def test_a_defederation_answer_that_is_json_but_not_an_object_is_a_failed_fetch(app, http_mock):
    """A peer answering `[]` where an object is expected is an answer this cannot read; None keeps the
    subscription's existing bans rather than clearing them."""
    from app.utils import instance_software
    make_instance(DEFED_DOMAIN, software='lemmy')
    http_mock.get(f'https://{DEFED_DOMAIN}/api/v3/federated_instances').respond(200, json=['nasty.test'])

    assert instance_software(DEFED_DOMAIN) == 'lemmy'
    assert retrieve_defederation_list(DEFED_DOMAIN) is None


def test_nodeinfo_that_is_not_json_falls_back_to_nodeinfo2(app, http_mock):
    """Castopod answers /.well-known/nodeinfo with an HTML 404 page and serves only NodeInfo2."""
    http_mock.get(f'https://{PEER}/.well-known/nodeinfo').respond(200, text='<html>not found</html>')
    http_mock.get(f'https://{PEER}/.well-known/x-nodeinfo2').respond(200, json={'server': {'software': 'Castopod'}})

    assert remote_instance_software(f'https://{PEER}') == 'castopod'


def test_nodeinfo2_that_is_not_json_is_reported_as_no_software(app, http_mock):
    http_mock.get(f'https://{PEER}/.well-known/nodeinfo').respond(200, text='<html>not found</html>')
    http_mock.get(f'https://{PEER}/.well-known/x-nodeinfo2').respond(200, text='<html>not found</html>')

    with pytest.raises(Exception, match='no nodeinfo 2.0 or 2.1 endpoint and no NodeInfo2 software'):
        remote_instance_software(f'https://{PEER}')


def test_a_read_with_no_time_left_does_not_start_another_request(app, http_mock):
    """The timeout caps every request together: once the budget is spent the next read is refused outright, not
    given a zero-second request."""
    with pytest.raises(httpx.HTTPError, match='no time left'):
        _nodeinfo_json(f'https://{PEER}/.well-known/nodeinfo', deadline=0.0)


def test_a_capped_read_that_was_too_large_or_too_slow_is_an_error(app, monkeypatch):
    monkeypatch.setattr('app.utils.get_request_capped', lambda *a, **k: (200, None))

    with pytest.raises(httpx.HTTPError, match='too large or too slow'):
        remote_instance_software(f'https://{PEER}', timeout=5)


def test_getmtime_of_a_static_file_that_does_not_exist_is_none(app):
    """Used by templates for cache-busting: a missing asset must render without a version, not raise."""
    assert getmtime('no-such-file-anywhere.css') is None
