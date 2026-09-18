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


def test_copying_a_feed_lets_a_non_admin_mint_an_instance_feed(app, db_session):
    """PIN (P1): :254 takes is_instance_feed straight from the form, with only
    the widget disabled at :234-235.

    THIS IS D675 AGAIN. Sub-project 50 repaired it in make_feed; feed_copy
    builds its Feed(...) inline and never reaches that code, so the defect
    survived the repair.
    """
    instance, owner = _seed()
    source = _feed(owner)
    assert not owner.is_admin()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.post(f'/feed/{source.id}/copy',
                                   data=_copy_payload(app, client, is_instance_feed='y'))

    assert response.status_code == 302
    assert Feed.query.filter_by(name='copiedfeed').one().is_instance_feed is True


def test_copying_a_feed_accepts_nsfw_flags_the_site_has_disabled(app, db_session):
    """PIN (P2): :251 takes both flags from the form with no check.

    THIS IS D702 AGAIN. Sub-project 52 repaired it in feed_new one commit range
    ago; this route does not even disable the widgets on the way in -- it
    disables them only on the GET re-render at :318-323.
    """
    instance, owner = _seed()
    source = _feed(owner)
    site = Site.query.get(1)
    site.enable_nsfw = False
    site.enable_nsfl = False
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.post(f'/feed/{source.id}/copy',
                                   data=_copy_payload(app, client, nsfw='y', nsfl='y'))

    assert response.status_code == 302
    made = Feed.query.filter_by(name='copiedfeed').one()
    assert made.nsfw is True and made.nsfl is True


def test_the_copy_form_prefills_nsfl_from_the_nsfw_column(app, db_session):
    """PIN (P3): :325 assigns copy_feed_form.nsfw.data = feed_to_copy.nsfw
    inside the NSFL branch.

    THIS IS D701 AGAIN, VERBATIM -- the same two-word slip sub-project 52
    repaired in feed_edit, in a function neither round opened. The source feed's
    two columns differ, asserted first, because with both equal no test can tell
    which was read.
    """
    instance, owner = _seed()
    source = _feed(owner, nsfw=False, nsfl=True)
    site = Site.query.get(1)
    site.enable_nsfw = site.enable_nsfl = True
    db.session.commit()
    assert source.nsfw is not source.nsfl

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get(f'/feed/{source.id}/copy')

    assert response.status_code == 200
    assert captured['form'].nsfw.data is False
    assert captured['form'].nsfl.data is False


def test_a_copied_feed_has_no_outbox_url(app, db_session):
    """PIN (P4): :255-262 builds ap_profile_id, ap_public_url, ap_followers_url
    and ap_following_url -- and stops.

    make_feed:226 sets ap_outbox_url as well, and
    app/activitypub/routes.py:2770 serves it as the "id" of the feed's outbox
    document, so a copied feed publishes an outbox whose id is null. The other
    four urls are asserted present in the same test, so this is a statement
    about the one that is missing rather than about the row being empty.
    """
    instance, owner = _seed()
    source = _feed(owner)

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            client.post(f'/feed/{source.id}/copy', data=_copy_payload(app, client))

    made = Feed.query.filter_by(name='copiedfeed').one()
    assert made.ap_profile_id and made.ap_public_url
    assert made.ap_followers_url and made.ap_following_url
    assert made.ap_outbox_url is None
