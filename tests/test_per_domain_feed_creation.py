"""A new, edited or copied feed is made on its creator's home domain, and a feed tree stays on one domain (plan F4, task 3)."""
import io
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g, session
from flask_wtf.csrf import generate_csrf

from app import db
from app.api.alpha.utils.feed import post_feed, put_feed
from app.constants import SRC_API, SRC_WEB
from app.feed.util import feeds_for_form
from app.models import Feed, FeedMember, Role, Site, user_role
from app.shared.feed import edit_feed, make_feed, parent_feed_on
from app.utils import feed_tree
from tests.three_domains import FIRST, ONION, SHOP, three_domains, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')

FIRST_URL = f'https://{FIRST}'


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        g.pop('csrf_token', None)   # a second token in one test is made afresh, not read from g
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def payload(app, client, **overrides):
    data = {'csrf_token': csrf(app, client), 'title': 'A feed', 'url': 'afeed', 'description': '',
            'communities': 'somecommunity@remote.example', 'public': 'y'}
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    data.setdefault('icon_file', (io.BytesIO(b''), ''))
    data.setdefault('banner_file', (io.BytesIO(b''), ''))
    return data


def post(app, user, path, **overrides):
    """POST `path` on the first domain's host as `user`."""
    with app.test_client() as client:
        login(client, user)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            return client.post(path, data=payload(app, client, **overrides), base_url=FIRST_URL)


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def make_admin(user):
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()


@pytest.fixture
def api_site(app, site):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.enable_nsfw = g.site.enable_nsfl = True
    db.session.commit()


def api_input(name, **overrides):
    data = {'url': name, 'title': name, 'public': True, 'description': '', 'icon_url': None, 'banner_url': None,
            'nsfw': False, 'nsfl': False, 'communities': '', 'is_instance_feed': False, 'show_child_posts': False,
            'parent_feed_id': None}
    data.update(overrides)
    return data


def test_a_feed_belongs_to_its_creators_home_domain(app, three_domains):
    shopper = three_domains.account('shopper', three_domains.shop)
    response = post(app, shopper, '/feed/new', url='shopnews')
    assert response.status_code == 302
    feed = Feed.query.filter_by(name='shopnews').one()
    assert feed.site_domain_id == three_domains.shop.id and feed.instance_id == three_domains.shop.instance_id
    assert feed.ap_profile_id == f'https://{SHOP}/f/shopnews' and feed.ap_domain == SHOP
    assert feed.ap_outbox_url == f'https://{SHOP}/f/shopnews/outbox'


def test_a_first_domain_feed_gets_exactly_todays_ids(app, three_domains):
    owner = three_domains.account('firster', three_domains.first)
    assert post(app, owner, '/feed/new', url='x').status_code == 302
    feed = Feed.query.filter_by(name='x').one()
    base = f'https://{FIRST}/f/x'
    assert (feed.ap_profile_id, feed.ap_public_url, feed.ap_followers_url, feed.ap_following_url, feed.ap_outbox_url,
            feed.ap_domain, feed.instance_id) == (base, base, f'{base}/followers', f'{base}/following',
                                                 f'{base}/outbox', FIRST, three_domains.first.instance_id)
    assert feed.instance_id == 1


def test_an_onion_creator_gets_onion_ids(app, three_domains, api_site):
    user = three_domains.account('hidden', three_domains.onion)
    feed = make_feed(api_input('x'), SRC_API, token(user))
    assert feed.ap_profile_id == f'http://{ONION}/f/x' and feed.ap_domain == ONION
    assert feed.site_domain_id == three_domains.onion.id and feed.instance_id == three_domains.onion.instance_id


def test_the_api_make_feed_follows_the_token_users_home(app, three_domains, api_site):
    shopper = three_domains.account('shopper', three_domains.shop)
    with patch('app.api.alpha.utils.feed.get_feed', return_value={}):
        post_feed(token(shopper), {'name': 'viaapi', 'title': 'Via API'})
    feed = Feed.query.filter_by(name='viaapi').one()
    assert feed.site_domain_id == three_domains.shop.id and feed.ap_profile_id == f'https://{SHOP}/f/viaapi'


def test_a_rename_keeps_the_feeds_home_domain(app, three_domains):
    admin = three_domains.account('boss', three_domains.first)
    make_admin(admin)
    shopper = three_domains.account('shopper', three_domains.shop)
    feed = three_domains.local_feed('oldname', three_domains.shop, owner=shopper)
    response = post(app, admin, f'/feed/{feed.id}/edit', url='renamed', title='Renamed')
    assert response.status_code == 302
    db.session.refresh(feed)
    assert feed.name == 'renamed'
    assert feed.site_domain_id == three_domains.shop.id and feed.ap_profile_id == f'https://{SHOP}/f/renamed'
    assert feed.ap_outbox_url == f'https://{SHOP}/f/renamed/outbox' and feed.ap_domain == SHOP
    assert feed.instance_id == three_domains.shop.instance_id


@pytest.fixture(params=['missing', 'abc', 'other_domain'])
def bad_parent(request, three_domains):
    """A parent id that names no feed, no integer, or a feed of another domain than the shop account's."""
    if request.param == 'missing':
        return '987654'
    if request.param == 'abc':
        return 'abc'
    return str(three_domains.local_feed('onfirst', three_domains.first).id)


def test_a_bad_parent_is_refused_on_the_web(app, three_domains, bad_parent):
    shopper = three_domains.account('shopper', three_domains.shop)
    own = three_domains.local_feed('own', three_domains.shop, owner=shopper)
    before = Feed.query.count()
    assert post(app, shopper, '/feed/new', url='child', parent_feed_id=bad_parent).status_code == 400
    assert post(app, shopper, f'/feed/{own.id}/edit', url='own', parent_feed_id=bad_parent).status_code == 400
    db.session.refresh(own)
    assert Feed.query.count() == before and own.parent_feed_id is None


def test_a_bad_parent_is_refused_on_the_web_copy(app, three_domains, bad_parent):
    shopper = three_domains.account('shopper', three_domains.shop)
    source = three_domains.local_feed('source', three_domains.shop, owner=shopper)
    before = Feed.query.count()
    assert post(app, shopper, f'/feed/{source.id}/copy', url='copied', parent_feed_id=bad_parent).status_code == 400
    assert Feed.query.count() == before


def test_a_bad_parent_is_refused_by_the_api(app, three_domains, api_site, bad_parent):
    shopper = three_domains.account('shopper', three_domains.shop)
    own = three_domains.local_feed('own', three_domains.shop, owner=shopper)
    before = Feed.query.count()
    parent = int(bad_parent) if bad_parent.isdigit() else bad_parent
    # the API's own owner-list check answers first (its existing 400, parent_feed_not_allowed); the shared
    # functions answer parent_feed_invalid for a caller that gets past it
    with pytest.raises(Exception, match='parent_feed_not_allowed'):
        post_feed(token(shopper), {'name': 'child', 'title': 'Child', 'parent_feed_id': parent})
    with pytest.raises(Exception, match='parent_feed_not_allowed'):
        put_feed(token(shopper), {'feed_id': own.id, 'parent_feed_id': parent})
    with pytest.raises(Exception, match='parent_feed_invalid'):
        make_feed(api_input('child', parent_feed_id=parent), SRC_API, token(shopper))
    with pytest.raises(Exception, match='parent_feed_invalid'):
        edit_feed(api_input('own', parent_feed_id=parent), own, SRC_API, token(shopper))
    db.session.refresh(own)
    assert Feed.query.count() == before and own.parent_feed_id is None


def test_a_refused_parent_changes_nothing_of_the_feed(app, three_domains, api_site, bad_parent):
    """edit_feed resolves the parent before it assigns anything: a raise does not roll back, so the next commit in the
    session would otherwise persist the rejected edit."""
    shopper = three_domains.account('shopper', three_domains.shop)
    own = three_domains.local_feed('own', three_domains.shop, owner=shopper)
    before = {column: getattr(own, column) for column in
              ('name', 'machine_name', 'title', 'description', 'ap_profile_id', 'ap_public_url', 'ap_followers_url',
               'ap_following_url', 'ap_outbox_url', 'ap_domain', 'parent_feed_id', 'show_posts_in_children')}
    parent = int(bad_parent) if bad_parent.isdigit() else bad_parent
    with pytest.raises(Exception, match='parent_feed_invalid'):
        edit_feed(api_input('renamed', title='Retitled', description='New words', show_child_posts=True,
                            parent_feed_id=parent), own, SRC_API, token(shopper))
    db.session.commit()   # what the next request in the session does
    db.session.expire_all()
    own = db.session.get(Feed, own.id)
    assert {column: getattr(own, column) for column in before} == before


def test_a_bad_parent_the_form_let_through_is_still_a_400(app, three_domains, bad_parent):
    """The form's choices are the first guard; make_feed's, edit_feed's and feed_copy's own check is the second."""
    shopper = three_domains.account('shopper', three_domains.shop)
    own = three_domains.local_feed('own', three_domains.shop, owner=shopper)
    source = three_domains.local_feed('source', three_domains.shop, owner=shopper)
    # a form that offers every id, 'abc' aside (the form coerces it to an integer)
    everything = [(0, 'None'), (987654, 'x')] + [(feed.id, feed.title) for feed in Feed.query.all()]
    parent = '987654' if bad_parent == 'abc' else bad_parent
    with patch('app.feed.routes.feeds_for_form', return_value=everything):
        statuses = [post(app, shopper, '/feed/new', url='child', parent_feed_id=parent).status_code,
                    post(app, shopper, f'/feed/{source.id}/copy', url='copied', parent_feed_id=parent).status_code,
                    post(app, shopper, f'/feed/{own.id}/edit', url='own', parent_feed_id=parent).status_code]
    assert statuses == [400, 400, 400]
    assert Feed.query.filter(Feed.name.in_(['child', 'copied'])).count() == 0


def test_parent_feed_on_accepts_none_zero_and_a_feed_of_the_domain(app, three_domains):
    mine = three_domains.local_feed('mine', three_domains.shop)
    assert parent_feed_on(three_domains.shop, None) is None
    assert parent_feed_on(three_domains.shop, 0) is None
    assert parent_feed_on(three_domains.shop, str(mine.id)) == mine.id
    for refused in (mine.id, 2 ** 40, -1):
        with pytest.raises(ValueError, match='parent_feed_invalid'):
            parent_feed_on(three_domains.first, refused)


def test_feed_copy_makes_the_copy_on_the_copiers_home_domain(app, three_domains):
    source_owner = three_domains.account('firster', three_domains.first)
    source = three_domains.local_feed('source', three_domains.first, owner=source_owner)
    copier = three_domains.account('shopper', three_domains.shop)
    parent = three_domains.local_feed('shopparent', three_domains.shop, owner=copier)
    response = post(app, copier, f'/feed/{source.id}/copy', url='shopcopy', parent_feed_id=str(parent.id))
    assert response.status_code == 302
    copy = Feed.query.filter_by(name='shopcopy').one()
    assert copy.site_domain_id == three_domains.shop.id and copy.instance_id == three_domains.shop.instance_id
    assert copy.ap_profile_id == f'https://{SHOP}/f/shopcopy' and copy.ap_domain == SHOP
    assert copy.parent_feed_id == parent.id
    assert Feed.query.filter_by(name='source').one().site_domain_id == three_domains.first.id


def test_parent_choices_are_the_feeds_own_domain_only(app, three_domains):
    owner = three_domains.account('shopper', three_domains.shop)
    on_shop = three_domains.local_feed('onshop', three_domains.shop, owner=owner)
    on_first = three_domains.local_feed('onfirst', three_domains.first, owner=owner)
    shop_child = three_domains.local_feed('shopchild', three_domains.shop, owner=owner)
    shop_child.parent_feed_id = on_shop.id
    db.session.commit()
    assert [feed_id for feed_id, _ in feeds_for_form(0, owner.id, three_domains.shop)] == [0, on_shop.id, shop_child.id]
    assert [feed_id for feed_id, _ in feeds_for_form(0, owner.id, three_domains.first)] == [0, on_first.id]
    assert {node['feed'].id for node in feed_tree(owner.id)} == {on_shop.id, on_first.id}
    assert [node['feed'].id for node in feed_tree(owner.id, three_domains.first)] == [on_first.id]
    assert [node['feed'].id for node in feed_tree(owner.id, three_domains.shop)] == [on_shop.id]


def test_a_feed_icon_is_uploaded_on_the_feeds_domain(app, three_domains, api_site, monkeypatch):
    shopper = three_domains.account('shopper', three_domains.shop)
    uploads = []

    def fake_upload(file, destination, owner):
        uploads.append((destination, owner))

    monkeypatch.setattr('app.shared.feed.process_upload', fake_upload)
    monkeypatch.setattr('app.shared.feed.current_user', shopper)
    fields = {'url': 'withicon', 'title': 'T', 'public': True, 'description': '', 'nsfw': False, 'nsfl': False,
              'communities': '', 'is_instance_feed': False, 'show_child_posts': False, 'parent_feed_id': 0}
    form = SimpleNamespace(**{key: SimpleNamespace(data=value) for key, value in fields.items()})
    with app.test_request_context():
        feed = make_feed(form, SRC_WEB, None, 'icon', 'banner')
        assert uploads == [('feeds', three_domains.shop)] * 2
        uploads.clear()
        edit_feed(form, feed, SRC_WEB, None, 'icon', 'banner')
    assert uploads == [('feeds', three_domains.shop)] * 2


def hit_copy(app, copier, feed_id, host=FIRST):
    with app.test_client() as client:
        login(client, copier)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            return client.get(f'/feed/{feed_id}/copy', base_url=f'https://{host}')


def test_a_copy_of_a_feed_the_copier_may_not_see_answers_as_a_missing_one(app, three_domains):
    owner = three_domains.account('firster', three_domains.first)
    copier = three_domains.account('stranger', three_domains.first)
    private = three_domains.local_feed('secret/firster', three_domains.first, owner=owner, public=False)
    public = three_domains.local_feed('open', three_domains.first, owner=owner)
    onion_owner = three_domains.account('hidden', three_domains.onion)
    onion_feed = three_domains.local_feed('onionfeed', three_domains.onion, owner=onion_owner)
    missing = hit_copy(app, copier, 987654)
    assert missing.status_code == 404
    assert hit_copy(app, copier, public.id).status_code == 200   # control: a visible feed is copyable
    for hidden in (private, onion_feed):   # the same answer as for a missing id: status and body
        answer = hit_copy(app, copier, hidden.id)
        assert (answer.status_code, answer.data) == (missing.status_code, missing.data)
    # a member of the private feed may copy it
    db.session.add(FeedMember(feed_id=private.id, user_id=copier.id, is_owner=False))
    db.session.commit()
    assert hit_copy(app, copier, private.id).status_code == 200
