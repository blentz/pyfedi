"""Feed names are unique per domain, and the API reads a bare feed name on the request's own host (plan F4, task 9;
spec 2026-10-26-per-domain-feeds goals 5 and 11, Review Focus 4).

Two domains may each have a feed called `news`, and a feed name blocks a user or community name on its own domain
only. Off the onion, no answer tells that an onion feed exists or that a name is taken there: the API answers an onion
feed exactly as one nobody holds, and no name check on another domain counts it."""
from unittest.mock import patch

import pytest
from wtforms.validators import ValidationError

from app import db
from app.api.alpha.utils.feed import get_feed
from app.api.alpha.utils.misc import get_resolve_object
from app.auth.forms import RegistrationForm
from app.constants import SRC_API
from app.feed.util import search_for_feed
from app.models import Feed
import app.shared.feed as shared_feed
from app.utils import user_name_is_taken
from tests.conftest import forget_login
from tests.factories import make_feed, make_user
import tests.test_per_domain_feed_creation as creation
from tests.test_per_domain_feed_creation import api_input, api_site, make_admin, token  # noqa: F401
from tests.test_per_domain_name_creation import _community_form, _feed_form
from tests.three_domains import FIRST, ONION, PEER, SHOP, no_http, peer, request_on, three_domains, \
    two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

MISSING = 'No object found.'


@pytest.fixture
def alpha(app, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')


def api(app, path, host, user=None, **query):
    """GET `/api/alpha<path>` on `host`, as `user` (None: no token)."""
    forget_login()
    headers = {'Authorization': f'Bearer {user.encode_jwt_token()}'} if user is not None else {}
    return app.test_client().get(f'/api/alpha{path}', base_url=f'https://{host}', headers=headers, query_string=query)


def registration_error(name):
    form = RegistrationForm(meta={'csrf': False})
    form.user_name.data = name
    try:
        form.validate_user_name(form.user_name)
    except ValidationError as error:
        return str(error)
    return None


def remote_feed(peer):
    """A remote feed, owned by a remote account, as the API's views need an owner."""
    feed = make_feed(peer, 'peerfeed', public=True)
    feed.user_id = make_user(peer, 'peerowner').id
    db.session.commit()
    return feed


def post(app, user, path, **overrides):
    """`creation.post`, answered as `user`: the app context outlives a request, and keeps the last caller."""
    forget_login()
    return creation.post(app, user, path, **overrides)


def url_errors(app, user, path, **overrides):
    """The url field's errors when the page `post` reaches renders its form again (a refusal), else None."""
    shown = {}

    def render(template, **context):
        shown.update(context)
        return 'rendered'
    with patch('app.feed.routes.render_template', side_effect=render):
        response = post(app, user, path, **overrides)
    if response.status_code != 200:
        return None
    return [str(message) for message in shown['form'].url.errors]


def local_feeds(name):
    return Feed.query.filter(db.func.lower(Feed.name) == name.lower(), Feed.ap_id.is_(None)).all()


# --- names ----------------------------------------------------------------------------------------------------------

def test_two_domains_can_each_create_a_feed_called_news(app, three_domains, api_site):
    d = three_domains
    firster, shopper = d.account('firster', d.first), d.account('shopper', d.shop)
    assert post(app, firster, '/feed/new', url='news').status_code == 302
    assert post(app, shopper, '/feed/new', url='news').status_code == 302
    assert sorted(feed.site_domain_id for feed in local_feeds('news')) == sorted([d.first.id, d.shop.id])
    # the API's create and a copy too
    onioner = d.account('onioner', d.onion)
    assert shared_feed.make_feed(api_input('news'), SRC_API, token(onioner)).site_domain_id == d.onion.id
    source = d.local_feed('source', d.first, firster)
    copier = d.account('copier', d.shop)
    assert post(app, copier, f'/feed/{source.id}/copy', url='source').status_code == 302
    assert sorted(feed.site_domain_id for feed in local_feeds('source')) == sorted([d.first.id, d.shop.id])


def test_a_same_domain_duplicate_is_still_refused_in_any_case(app, three_domains, api_site):
    d = three_domains
    shopper, other = d.account('shopper', d.shop), d.account('other', d.shop)
    d.local_feed('news', d.shop, shopper)
    assert _feed_form(app, other, 'News') == (False, ['A Feed with this url already exists.'])
    assert url_errors(app, other, '/feed/new', url='News') == ['A Feed with this url already exists.']
    with pytest.raises(Exception, match='A feed with that url already exists'):
        shared_feed.make_feed(api_input('NEWS'), SRC_API, token(other))
    source = d.local_feed('source', d.first)
    assert url_errors(app, other, f'/feed/{source.id}/copy', url='News') == ['A Feed with this url already exists.']
    # the copy route's own check, behind the form's, is on the copier's home too
    with patch('app.feed.routes.AddCopyFeedForm.validate', new=lambda self, extra_validators=None: True):
        response = post(app, other, f'/feed/{source.id}/copy', url='News')
    assert response.status_code == 302 and response.location.endswith(f'/feed/{source.id}/copy')
    assert len(local_feeds('news')) == 1


def test_a_feed_name_does_not_block_a_user_or_community_on_another_domain(app, three_domains):
    d = three_domains
    d.local_feed('news', d.shop)
    assert not user_name_is_taken('news', d.first.id)
    with request_on(app, d.first):
        assert registration_error('news') is None
    firster = d.account('firster', d.first)
    assert _community_form(app, firster, 'news') == (True, [])


def test_an_onion_feed_name_blocks_no_first_domain_name(app, three_domains):
    """No name check off the onion tells that a name is taken there (Review Focus 4)."""
    d = three_domains
    d.local_feed('secret', d.onion, d.account('onioner', d.onion))
    assert not user_name_is_taken('secret', d.first.id) and not user_name_is_taken('secret', None)
    with request_on(app, d.first):
        assert registration_error('Secret') is None
    firster = d.account('firster', d.first)
    assert _community_form(app, firster, 'secret') == (True, [])
    assert _feed_form(app, firster, 'secret') == (True, [])


def test_a_feed_name_still_blocks_a_user_and_community_on_its_own_domain(app, three_domains):
    d = three_domains
    d.local_feed('news', d.shop)
    assert user_name_is_taken('NEWS', d.shop.id)
    with request_on(app, d.shop):
        assert registration_error('News') == 'This name is in use already.'
    shopper = d.account('shopper', d.shop)
    assert _community_form(app, shopper, 'news') == (False, ['This name is in use already.'])


def test_a_rename_is_checked_on_the_feeds_own_domain(app, three_domains):
    d = three_domains
    admin = d.account('boss', d.first)
    make_admin(admin)
    shopper = d.account('shopper', d.shop)
    d.local_feed('news', d.first)        # a first-domain name does not stop a shop feed taking it
    d.local_feed('taken', d.shop)        # a shop name does
    feed = d.local_feed('oldname', d.shop, owner=shopper)
    assert url_errors(app, admin, f'/feed/{feed.id}/edit', url='Taken', title='Taken') == \
        ['A Feed with this url already exists.']
    db.session.refresh(feed)
    assert feed.name == 'oldname'
    assert post(app, admin, f'/feed/{feed.id}/edit', url='news', title='News').status_code == 302
    db.session.refresh(feed)
    assert feed.name == 'news' and feed.site_domain_id == d.shop.id


def renamed_columns(feed_id):
    db.session.expire_all()
    feed = db.session.get(Feed, feed_id)
    return {column: getattr(feed, column) for column in ('name', 'machine_name', 'title', 'ap_profile_id', 'ap_public_url',
                                                         'ap_outbox_url', 'ap_domain', 'site_domain_id')}


def api_put(app, user, host=SHOP, **body):
    forget_login()
    return app.test_client().put('/api/alpha/feed', base_url=f'https://{host}', json=body,
                                 headers={'Authorization': f'Bearer {user.encode_jwt_token()}'})


@pytest.mark.parametrize('url', ['news', 'News', 'news_', 'news-'])
def test_an_api_rename_to_a_name_its_domain_holds_is_a_400(app, alpha, three_domains, api_site, url):
    """Review I1: a rename collision was an IntegrityError at commit, a 500. It is refused before anything changes."""
    d = three_domains
    shopper = d.account('shopper', d.shop)
    d.local_feed('news', d.shop, shopper)
    feed = d.local_feed('oldname', d.shop, shopper)
    before = renamed_columns(feed.id)
    response = api_put(app, shopper, feed_id=feed.id, url=url, title='Renamed')
    assert response.status_code == 400 and b'A feed with that url already exists' in response.data
    assert renamed_columns(feed.id) == before


def test_an_api_rename_to_a_name_another_domain_holds_succeeds(app, alpha, three_domains, api_site):
    d = three_domains
    shopper = d.account('shopper', d.shop)
    d.local_feed('news', d.first, d.account('firster', d.first))
    feed = d.local_feed('oldname', d.shop, shopper)
    response = api_put(app, shopper, host=FIRST, feed_id=feed.id, url='News', title='News')
    assert response.status_code == 200, response.data
    assert renamed_columns(feed.id)['name'] == 'news' and renamed_columns(feed.id)['site_domain_id'] == d.shop.id


@pytest.mark.parametrize('url, error', [('News', 'A Feed with this url already exists.'),
                                        ('news_', 'A Feed with this url already exists.'),
                                        ('news__', 'A Feed with this url already exists.'),
                                        ('news-', '- cannot be in Url. Use _ instead?')])
def test_a_web_rename_to_a_name_its_domain_holds_shows_the_form_again(app, three_domains, url, error):
    """Review I1: the edit form checks the slug edit_feed stores, so `news_` is `news` and a form error, not a 500."""
    d = three_domains
    shopper = d.account('shopper', d.shop)
    d.local_feed('news', d.shop, shopper)
    feed = d.local_feed('oldname', d.shop, shopper)
    before = renamed_columns(feed.id)
    assert url_errors(app, shopper, f'/feed/{feed.id}/edit', url=url, title='Renamed') == [error]
    assert renamed_columns(feed.id) == before


def test_a_feed_keeps_its_own_name_when_edited(app, three_domains):
    d = three_domains
    shopper = d.account('shopper', d.shop)
    feed = d.local_feed('mine', d.shop, owner=shopper)
    assert post(app, shopper, f'/feed/{feed.id}/edit', url='mine', title='Retitled').status_code == 302
    db.session.refresh(feed)
    assert (feed.name, feed.title) == ('mine', 'Retitled')


# --- get_feed -------------------------------------------------------------------------------------------------------

@pytest.fixture
def news(app, three_domains, peer):  # noqa: F811
    d = three_domains
    rows = {'first': d.local_feed('news', d.first, d.account('firstowner', d.first)),
            'shop': d.local_feed('news', d.shop, d.account('shopowner', d.shop)),
            'remote': remote_feed(peer)}
    onioner = d.account('onioner', d.onion)
    rows['onion'] = d.local_feed('onionfeed', d.onion, onioner)
    rows['private'] = d.local_feed('privatefeed', d.shop, d.account('keeper', d.shop), public=False)
    return rows


@pytest.mark.parametrize('name, host, expected', [('news', SHOP, 'shop'), ('news', FIRST, 'first'),
                                                  (f'news@{FIRST}', SHOP, 'first'), (f'NEWS@{SHOP}', FIRST, 'shop'),
                                                  (f'peerfeed@{PEER}', FIRST, 'remote')])
def test_get_feed_by_name_follows_the_request_host(app, alpha, news, name, host, expected):
    response = api(app, '/feed', host, name=name)
    assert response.status_code == 200, response.data
    assert response.json['id'] == news[expected].id


@pytest.mark.parametrize('host', [FIRST, SHOP])
@pytest.mark.parametrize('query, missing', [({'name': f'onionfeed@{ONION}'}, {'name': f'nosuchfeed@{ONION}'}),
                                            ({'name': 'onionfeed'}, {'name': 'nosuchfeed'}),
                                            ({'id': 'ONION_ID'}, {'id': 987654}),
                                            ({'name': 'privatefeed'}, {'name': 'nosuchfeed'})])
def test_get_feed_off_the_onion_never_returns_an_onion_feed(app, alpha, news, three_domains, host, query, missing):
    """Hidden, private to a non-member and missing are one answer (ruling 3), whoever asks."""
    if query.get('id') == 'ONION_ID':
        query = {'id': news['onion'].id}
    if query.get('name') == 'privatefeed' and host == FIRST:
        query = {'name': f'privatefeed@{SHOP}'}
    for user in (None, three_domains.account(f'reader{host.split(".")[0]}', three_domains.first)):
        hidden, nobody = api(app, '/feed', host, user, **query), api(app, '/feed', host, user, **missing)
        assert hidden.status_code == 400
        assert (hidden.status_code, hidden.data) == (nobody.status_code, nobody.data)
        assert b'feed_not_found' in hidden.data


@pytest.mark.parametrize('name', ['a@b@c', '@', 'news@', '@@'])
def test_a_feed_name_with_junk_is_a_bad_request(app, alpha, news, name):
    response = api(app, '/feed', SHOP, name=name)
    assert response.status_code == 400


def test_more_than_one_at_sign_is_an_invalid_request(app, three_domains):
    with request_on(app, three_domains.shop):
        with pytest.raises(Exception, match='invalid_request'):
            get_feed(None, {'name': 'a@b@c'})


def test_get_feed_on_the_onion_finds_its_feed_for_a_signed_in_reader(app, news, three_domains):
    with request_on(app, three_domains.onion):
        reader = three_domains.account('onionreader', three_domains.onion)
        assert get_feed(None, {'name': 'onionfeed'}, user_id=reader.id)['id'] == news['onion'].id
        with pytest.raises(Exception, match='feed_not_found'):
            get_feed(None, {'name': 'onionfeed'})   # logged out on the onion: C2


# --- the feed list --------------------------------------------------------------------------------------------------

def listed(response):
    assert response.status_code == 200, response.data
    return sorted((feed['name'], feed['ap_domain']) for feed in response.json['feeds'])


def test_the_feed_list_api_follows_the_storefront_and_token_user(app, alpha, three_domains, peer):  # noqa: F811
    d = three_domains
    d.local_feed('firstnews', d.first, d.account('firstowner', d.first))
    d.local_feed('shopnews', d.shop, d.account('shopowner', d.shop))
    d.local_feed('onionfeed', d.onion, d.account('onioner', d.onion))
    remote_feed(peer)
    remote = ('peerfeed', PEER)
    assert listed(api(app, '/feed/list', SHOP)) == sorted([('shopnews', SHOP), remote])
    assert listed(api(app, '/feed/list', FIRST)) == sorted([('firstnews', FIRST), remote])
    firster = d.account('firster', d.first)
    assert listed(api(app, '/feed/list', SHOP, firster)) == sorted([('firstnews', FIRST), remote])
    shopper = d.account('shopper', d.shop)
    assert listed(api(app, '/feed/list', FIRST, shopper)) == sorted([('shopnews', SHOP), remote])


def test_mine_only_lists_the_token_users_own_feeds(app, alpha, three_domains):
    d = three_domains
    shopper = d.account('shopper', d.shop)
    d.local_feed('mine', d.shop, shopper)
    d.local_feed('notmine', d.shop, d.account('other', d.shop))
    assert listed(api(app, '/feed/list', FIRST, shopper, mine_only='true')) == [('mine', SHOP)]


# --- resolve_object -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('host', [FIRST, SHOP])
@pytest.mark.parametrize('query, missing', [
    (f'http://{ONION}/f/onionfeed', f'http://{ONION}/f/nosuchfeed'),                      # the ap_profile_id
    (f'http://{ONION}/f/onionfeed@{ONION}', f'http://{ONION}/f/nosuchfeed@{ONION}'),      # a URL naming it
    (f'https://{FIRST}/f/onionfeed@{ONION}', f'https://{FIRST}/f/nosuchfeed@{ONION}'),
    (f'http://{ONION}:80/f/onionfeed', f'http://{ONION}:80/f/nosuchfeed'),
    (f'~onionfeed@{ONION}', f'~nosuchfeed@{ONION}'),
])
def test_resolve_object_never_returns_an_onion_feed_off_the_onion(app, alpha, news, three_domains, no_http,  # noqa: F811
                                                                  host, query, missing):
    """Review Focus 4: by `ap_profile_id`, by URL and by handle, an onion feed answers off the onion exactly as a
    feed nobody holds, to every reader, an admin too."""
    admin = three_domains.account(f'boss{host.split(".")[0]}', three_domains.first)
    make_admin(admin)
    for user in (three_domains.account(f'reader{host.split(".")[0]}', three_domains.first), admin):
        hidden, nobody = api(app, '/resolve_object', host, user, q=query), \
            api(app, '/resolve_object', host, user, q=missing)
        assert hidden.status_code == 400, hidden.data
        assert (hidden.status_code, hidden.data) == (nobody.status_code, nobody.data)
        assert MISSING.encode() in hidden.data


@pytest.mark.parametrize('query, missing', [(f'https://{SHOP}/f/privatefeed', f'https://{SHOP}/f/nosuchfeed'),
                                            (f'https://{SHOP}:443/f/privatefeed', f'https://{SHOP}:443/f/nosuchfeed'),
                                            (f'~privatefeed@{SHOP}', f'~nosuchfeed@{SHOP}')])
def test_resolve_object_answers_a_private_feed_as_a_missing_one(app, alpha, news, three_domains, query, missing):
    """Review M5: a private feed, to a reader who is not its owner, is as unknown as a feed nobody holds."""
    reader = three_domains.account('stranger', three_domains.shop)
    hidden, nobody = api(app, '/resolve_object', SHOP, reader, q=query), api(app, '/resolve_object', SHOP, reader,
                                                                             q=missing)
    assert hidden.status_code == 400, hidden.data
    assert (hidden.status_code, hidden.data) == (nobody.status_code, nobody.data)


def test_resolve_object_finds_an_onion_feed_on_the_onion(app, news, three_domains):
    """The positive control: a signed-in onion reader resolves it by every spelling."""
    with request_on(app, three_domains.onion):
        reader = three_domains.account('onionreader', three_domains.onion)
        for query in (f'http://{ONION}/f/onionfeed', f'http://{ONION}/f/onionfeed@{ONION}', f'~onionfeed@{ONION}'):
            assert get_resolve_object(None, {'q': query}, user_id=reader.id)['feed']['id'] == news['onion'].id


@pytest.mark.parametrize('query, expected', [(f'https://{SHOP}/f/news', 'shop'), (f'https://{FIRST}/f/news', 'first'),
                                             (f'https://{FIRST}/f/news@{SHOP}', 'shop'),
                                             # not the stored ap_profile_id: read by its path, on the URL's domain
                                             (f'https://{SHOP}:443/f/news', 'shop'),
                                             (f'https://{FIRST}:443/f/news', 'first'),
                                             (f'https://{SHOP}:8443/f/news', 'shop')])   # review M2: any port
@pytest.mark.parametrize('host', [FIRST, SHOP])
def test_resolve_object_by_url_names_the_urls_domain(app, alpha, news, three_domains, query, expected, host):
    reader = three_domains.account(f'reader{host.split(".")[0]}', three_domains.first)
    response = api(app, '/resolve_object', host, reader, q=query)
    assert response.status_code == 200, response.data
    assert response.json['feed']['id'] == news[expected].id


def test_search_for_feed_takes_signed_in_from_its_caller(app, news, three_domains):
    """Task 7 review M5: a logged-out caller on the onion is not given an onion feed."""
    with request_on(app, three_domains.onion):
        assert search_for_feed(f'~onionfeed@{ONION}', allow_fetch=False, signed_in=True) == news['onion']
        assert search_for_feed(f'~onionfeed@{ONION}', allow_fetch=False, signed_in=False) is None
    with request_on(app, three_domains.first):
        assert search_for_feed(f'~onionfeed@{ONION}', allow_fetch=False, signed_in=True) is None
