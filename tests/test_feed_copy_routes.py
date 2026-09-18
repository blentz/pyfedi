"""app/feed/routes.py's feed_copy, feed_add_remote and lookup.

MEASUREMENT BASIS. Before this file existed the three carried 109 missing
statements and 61 missing arcs on the full-suite --cov=app run at 49c65baa.

WHAT THIS FILE IS ABOUT, BEYOND COVERAGE. feed_copy is the FOURTH
implementation of "create a feed" in this codebase -- make_feed, edit_feed,
feed_new's route work, and this one -- and it is the one no round has repaired.
Three of the four defects this file pins are defects the campaign already
closed elsewhere: is_instance_feed taken from the caller (D675, repaired in
make_feed), NSFW/NSFL taken past the site's switches (D702, repaired in
feed_new), and the NSFL pre-fill reading the NSFW column (D701, repaired in
feed_edit -- here it is verbatim).

HARNESS FACTS (fact 292-294 apply, plus one of this round's own):

- The copy POST must be MULTIPART. :268 and :273 read request.files['icon_file']
  and ['banner_file'] directly, so a urlencoded POST is a bare 400 before any
  form logic runs. Both parts are sent as empty files.
- render_template must be patched for every GET and for any POST that falls
  through to a re-render.
- search_for_feed is patched on app.feed.routes, where it is imported.
"""
import io
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.constants import SUBSCRIPTION_MEMBER
from app.models import Community, CommunityMember, Feed, FeedItem, FeedMember, \
    Role, Site, User, user_role
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    owner = make_user(instance, 'feedowner', local=True)
    return instance, owner


def _make_admin(user):
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()


def _feed(user, name='sourcefeed', **kwargs):
    kwargs.setdefault('public', True)
    feed = Feed(user_id=user.id, title=name, name=name, machine_name=name,
                ap_profile_id=f'https://test.piefed.local/f/{name}',
                ap_public_url=f'https://test.piefed.local/f/{name}',
                instance_id=1, subscriptions_count=1, **kwargs)
    db.session.add(feed)
    db.session.commit()
    return feed


def _copy_payload(app, client, **overrides):
    """The copy form's fields, as a MULTIPART body.

    The two file parts are not decoration: without them the route 400s at
    request.files['icon_file'] before the form is looked at.
    """
    data = {'csrf_token': csrf(app, client), 'title': 'Copied', 'url': 'copiedfeed',
            'description': '', 'communities': 'somecommunity@remote.example',
            'public': 'y'}
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    data.setdefault('icon_file', (io.BytesIO(b''), ''))
    data.setdefault('banner_file', (io.BytesIO(b''), ''))
    return data


def _capture_form():
    captured = {}

    def fake_render(template, **kwargs):
        captured['template'] = template
        captured.update(kwargs)
        return 'rendered'

    return captured, fake_render


# --------------------------------------------------------------------------
# Task 1: the four repairs, three of which repeat entries closed elsewhere.
# --------------------------------------------------------------------------


@pytest.mark.parametrize('is_admin', [False, True])
def test_copying_a_feed_lets_only_an_admin_mint_an_instance_feed(app, db_session, is_admin):
    """Was a PIN; INVERTED once the copy route gained the admin check.

    ORIGINAL PINNED CLAIM, now false: ":254 takes is_instance_feed straight from
    the form, with only the widget disabled at :234-235."

    THIS WAS D675 AGAIN. Sub-project 50 repaired it in make_feed; feed_copy
    builds its Feed(...) inline and never reaches that code, so the defect
    survived the repair by nine commits and two rounds.

    The admin row is the control: without it, a fix that cleared the flag for
    everybody would pass.
    """
    instance, owner = _seed()
    source = _feed(owner)
    if is_admin:
        _make_admin(owner)
    assert owner.is_admin() is is_admin

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.post(f'/feed/{source.id}/copy',
                                   data=_copy_payload(app, client, is_instance_feed='y'))

    assert response.status_code == 302
    assert Feed.query.filter_by(name='copiedfeed').one().is_instance_feed is is_admin


@pytest.mark.parametrize('site_nsfw, site_nsfl', [
    (False, False),
    (True, True),
    (True, False),
])
def test_copying_a_feed_honours_the_sites_nsfw_switches(app, db_session,
                                                        site_nsfw, site_nsfl):
    """Was a PIN; INVERTED once the copy route gained the site check.

    ORIGINAL PINNED CLAIM, now false: ":251 takes both flags from the form with
    no check." THIS WAS D702 AGAIN -- repaired in feed_new one round earlier,
    and this route does not even disable the widgets on the way in; it disables
    them only on the GET re-render.

    Three rows for the reason sub-project 52 gave: a repair that cleared both
    flags whenever either switch was off passes a two-row test.
    """
    instance, owner = _seed()
    source = _feed(owner)
    site = Site.query.get(1)
    site.enable_nsfw = site_nsfw
    site.enable_nsfl = site_nsfl
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.post(f'/feed/{source.id}/copy',
                                   data=_copy_payload(app, client, nsfw='y', nsfl='y'))

    assert response.status_code == 302
    made = Feed.query.filter_by(name='copiedfeed').one()
    assert made.nsfw is site_nsfw
    assert made.nsfl is site_nsfl


@pytest.mark.parametrize('site_nsfl, expected_nsfl', [(True, True), (False, False)])
def test_the_copy_form_prefills_nsfl_from_the_nsfl_column(app, db_session, site_nsfl,
                                                          expected_nsfl):
    """Was a PIN; INVERTED once the pre-fill read the right column.

    ORIGINAL PINNED CLAIM, now false: ":325 assigns copy_feed_form.nsfw.data =
    feed_to_copy.nsfw inside the NSFL branch." THIS WAS D701 AGAIN, VERBATIM --
    the same two-word slip, in a function neither round opened.

    Both site arms are rows, for the reason sub-project 52's version of this
    test had to learn the hard way: with the switch off the route takes the
    render_kw arm, the field keeps BooleanField's unbound default False, and a
    test run only in that state proves nothing.
    """
    instance, owner = _seed()
    source = _feed(owner, nsfw=False, nsfl=True)
    site = Site.query.get(1)
    site.enable_nsfw = True
    site.enable_nsfl = site_nsfl
    db.session.commit()
    assert source.nsfw is not source.nsfl

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get(f'/feed/{source.id}/copy')

    assert response.status_code == 200
    assert captured['form'].nsfw.data is False
    assert captured['form'].nsfl.data is expected_nsfl


def test_a_copied_feed_carries_the_same_five_urls_as_a_new_one(app, db_session):
    """Was a PIN; INVERTED once the copy route built the outbox url.

    ORIGINAL PINNED CLAIM, now false: ":255-262 builds ap_profile_id,
    ap_public_url, ap_followers_url and ap_following_url -- and stops", so a
    copied feed published an outbox document whose id was null
    (app/activitypub/routes.py:2770 serves that value).

    All five are asserted as exact strings rather than as non-None: the whole
    defect was one url missing from a block that built four correctly.
    """
    instance, owner = _seed()
    source = _feed(owner)
    server = app.config['SERVER_NAME']

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            client.post(f'/feed/{source.id}/copy', data=_copy_payload(app, client))

    made = Feed.query.filter_by(name='copiedfeed').one()
    assert made.ap_profile_id == f'https://{server}/f/copiedfeed'
    assert made.ap_public_url == f'https://{server}/f/copiedfeed'
    assert made.ap_followers_url == f'https://{server}/f/copiedfeed/followers'
    assert made.ap_following_url == f'https://{server}/f/copiedfeed/following'
    assert made.ap_outbox_url == f'https://{server}/f/copiedfeed/outbox'
    assert made.ap_domain == server
