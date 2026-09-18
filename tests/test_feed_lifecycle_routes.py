"""app/feed/routes.py's lifecycle routes: feed_new, feed_edit, feed_delete,
feed_notification and feed_unsubscribe.

MEASUREMENT BASIS. Before this file existed these five carried 115 missing
statements and 63 missing arcs on the full-suite --cov=app run at e82cdd98.
They were not untouched: eleven tests in tests/test_redirect_back.py execute
feed_delete and the subscribe route, which is why feed_delete shows one missing
statement. Those tests are about back()'s referrer policy -- where the user is
sent, not what the route did -- and this file is about the rest.

THREE HARNESS FACTS, each established by execution during scoping:

1. Feed templates cannot be rendered under the test config. Any GET that
   re-renders a form raises jinja2.exceptions.UndefinedError: '...
   AddCopyFeedForm object' has no attribute 'csrf_token', because the config
   disables CSRF for forms. Patch app.feed.routes.render_template and assert on
   the `form` it was handed -- which is also the only way to observe the NSFL
   pre-fill defect at all.
2. AddCopyFeedForm.communities is REQUIRED, and a real value sends
   form_communities_to_ids into search_for_community, which attempts a live
   webfinger and trips respx. Patch app.shared.feed.form_communities_to_ids.
3. apply_feed_url_rules rejects any url containing a slash on both the public
   and private arms, so no POST can reach feed_new's '/f/' prefix strip. Those
   two statements are dead code -- fact 75 cause 5 -- and this round leaves them
   uncovered with the proof recorded rather than faking the precondition.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Community, CommunityMember, Feed, FeedItem, FeedJoinRequest, \
    FeedMember, NotificationSubscription, Site, Topic, User
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')


def login(client, user):
    """The session cookie flask_login reads. Copied from
    tests/test_redirect_back.py:32 rather than imported, as the campaign's other
    route files do."""
    with client.session_transaction() as sess:
        sess['_user_id'] = str(user.id)
        sess['_fresh'] = True


def csrf(app, client):
    """A CSRF token/session pair the app will accept.

    app.utils.login_required calls flask_wtf's validate_csrf directly on every
    POST, which ignores WTF_CSRF_ENABLED -- so a POST route test needs a real
    token even though the test config disables CSRF for forms.
    """
    with app.test_request_context():
        token = generate_csrf()
        raw = session['csrf_token']
    with client.session_transaction() as sess:
        sess['csrf_token'] = raw
    return token


def _seed():
    """An owner, a stranger, and the id-1 seat burned.

    app/models.py:1259-1261 treats User id 1 as an admin, and three of these
    routes branch on is_admin(), so the seat is burned and the burn asserted.
    """
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    owner = make_user(instance, 'feedowner', local=True)
    stranger = make_user(instance, 'stranger', local=True)
    return instance, owner, stranger


def _feed(user, name='lifecyclefeed', **kwargs):
    feed = Feed(user_id=user.id, title=name, name=name, machine_name=name,
                ap_profile_id=f'https://test.piefed.local/f/{name}',
                ap_public_url=f'https://test.piefed.local/f/{name}',
                instance_id=1, public=True, subscriptions_count=1, **kwargs)
    db.session.add(feed)
    db.session.commit()
    return feed


def _capture_form():
    """A render_template stand-in that keeps the form it was handed."""
    captured = {}

    def fake_render(template, **kwargs):
        captured['template'] = template
        captured['form'] = kwargs.get('form')
        return 'rendered'

    return captured, fake_render


# --------------------------------------------------------------------------
# Task 1: P1 -- the edit form's NSFL pre-fill.
# --------------------------------------------------------------------------


def test_edit_form_prefills_nsfl_from_the_nsfw_column(app, db_session):
    """PIN (P1): :178-181's else arm assigns `edit_feed_form.nsfw.data =
    feed_to_edit.nsfw` -- inside the NSFL branch. `nsfl.data` is therefore never
    populated, so the edit form renders the NSFL box unchecked whatever the feed
    says, and because the form round-trips, saving any edit to an NSFL feed
    clears the flag.

    The row carries DIFFERENT values for the two columns, asserted before the
    request: with both equal, a test cannot tell which column the pre-fill read.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner, nsfw=False, nsfl=True)
    assert feed.nsfw is not feed.nsfl

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get(f'/feed/{feed.id}/edit')

    assert response.status_code == 200
    form = captured['form']
    assert form.nsfw.data is False
    assert form.nsfl.data is False


# --------------------------------------------------------------------------
# Task 2: P2 -- NSFW/NSFL the site forbids.
# --------------------------------------------------------------------------


def _create_payload(app, client, **overrides):
    data = {'csrf_token': csrf(app, client), 'title': 'A feed', 'url': 'afeed',
            'description': '', 'communities': 'somecommunity@remote.example',
            'public': 'y'}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


def test_creating_a_feed_accepts_nsfw_flags_the_site_has_disabled(app, db_session):
    """PIN (P2): :46-49 disable the two widgets when the site has NSFW or NSFL
    off. That is a browser-side hint -- nothing checks the submitted values, and
    make_feed writes them unconditionally, unlike edit_feed:377-380, which
    guards both writes behind g.site.

    The same shape as D675 and D696: the third time this campaign has found
    `render_kw = {'disabled': True}` standing in for a server-side rule.

    form_communities_to_ids is patched because the form requires a communities
    value and a real one reaches search_for_community, which attempts a live
    webfinger.
    """
    instance, owner, stranger = _seed()
    site = Site.query.get(1)
    site.enable_nsfw = False
    site.enable_nsfl = False
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            response = client.post('/feed/new', data=_create_payload(
                app, client, url='spicy', nsfw='y', nsfl='y'))

    assert response.status_code == 302
    made = Feed.query.filter_by(name='spicy').one()
    assert made.nsfw is True
    assert made.nsfl is True


# --------------------------------------------------------------------------
# Task 3: P3 -- ?topic_id= naming a topic that is not there.
# --------------------------------------------------------------------------


def test_creating_a_feed_from_a_missing_topic_raises(app, db_session):
    """PIN (P3): :67-68 does Topic.query.get(request.args.get('topic_id')) and
    reads topic.communities with no check, so a topic id that resolves to
    nothing is a 500.

    The exception is asserted rather than a status code: the test client
    re-raises by default, so the 500 never reaches a response object. The query
    string is user-supplied, and the link carrying it comes from a page that may
    have been open while the topic was deleted.
    """
    instance, owner, stranger = _seed()
    assert Topic.query.get(999) is None

    with app.test_client() as client:
        login(client, owner)
        with pytest.raises(AttributeError, match='communities'):
            client.get('/feed/new?topic_id=999')


# --------------------------------------------------------------------------
# Task 4: P4 -- unsubscribing without telling the communities.
# --------------------------------------------------------------------------


def _subscribed_feed_with_community(owner, member, joined_via_feed=True,
                                    subscriptions_count=5):
    """A feed the member subscribes to, carrying one REMOTE community the
    member joined through it.

    The community is remote (ap_id set, its own instance) because the shared
    leave_community federates an Undo Follow for exactly that case, and the
    point of P4 is that this route never does.
    """
    feed = _feed(owner, name='subscribedfeed')
    remote_instance = make_instance('remote.example', software='lemmy')
    community = Community(name='inthefeed', title='In the feed',
                          instance_id=remote_instance.id,
                          ap_profile_id='https://remote.example/c/inthefeed',
                          ap_public_url='https://remote.example/c/inthefeed',
                          ap_inbox_url='https://remote.example/c/inthefeed/inbox',
                          ap_id='inthefeed@remote.example',
                          subscriptions_count=subscriptions_count)
    db.session.add(community)
    db.session.commit()
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    db.session.add(FeedMember(feed_id=feed.id, user_id=member.id))
    db.session.add(CommunityMember(user_id=member.id, community_id=community.id,
                                   joined_via_feed=joined_via_feed))
    feed.subscriptions_count = 2
    member.feed_auto_leave = True
    db.session.commit()
    return feed, community


def test_unsubscribing_drops_the_community_without_telling_it(app, db_session):
    """PIN (P4): :640-641 deletes the CommunityMember row by hand. The shared
    leave_community (app/shared/community.py:57-84) dispatches
    task_selector('leave_community', ...) -- which federates the Undo Follow --
    and the route does not.

    So the membership disappears locally, the remote community is never told,
    and its subscriptions_count is left one too high. The count is seeded to 5
    rather than 1 so "unchanged" is a value rather than a coincidence.
    """
    instance, owner, member = _seed()
    feed, community = _subscribed_feed_with_community(owner, member)

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.send_post_request') as send:
            response = client.get(f'/feed/{feed.name}/unsubscribe')

    assert response.status_code == 302
    assert CommunityMember.query.filter_by(user_id=member.id,
                                           community_id=community.id).count() == 0
    assert send.call_count == 0
    assert Community.query.get(community.id).subscriptions_count == 5
