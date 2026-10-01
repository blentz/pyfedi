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

from flask import render_template, session
from flask_login import login_user
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
    kwargs.setdefault('public', True)
    kwargs.setdefault('subscriptions_count', 1)
    feed = Feed(user_id=user.id, title=name, name=name, machine_name=name,
                ap_profile_id=f'https://test.piefed.local/f/{name}',
                ap_public_url=f'https://test.piefed.local/f/{name}',
                instance_id=1, **kwargs)
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


@pytest.mark.parametrize('site_nsfl, expected_nsfl', [(True, True), (False, False)])
def test_edit_form_prefills_nsfl_from_the_nsfl_column(app, db_session, site_nsfl,
                                                      expected_nsfl):
    """Was a PIN; INVERTED once the pre-fill read the right column.

    ORIGINAL PINNED CLAIM, now false: ":178-181's else arm assigns
    `edit_feed_form.nsfw.data = feed_to_edit.nsfw` -- inside the NSFL branch",
    so nsfl.data was never populated, the NSFL box rendered unchecked whatever
    the feed said, and saving any edit cleared the flag.

    THE FIRST VERSION OF THIS PIN PASSED FOR THE WRONG REASON, and correcting it
    is why the parametrisation exists. `Site.enable_nsfl` defaults False on the
    row the `site` fixture mints (fact 291), so the route took the render_kw arm
    and never reached the assignment at all -- the pin asserted `nsfl.data is
    False` against an untouched field and would have passed against the repair
    too. The site switch is now set explicitly, and both arms are rows:

    - switch ON  -> the else arm runs and nsfl.data is the feed's nsfl (True);
    - switch OFF -> the widget is disabled and nsfl.data keeps BooleanField's
      unbound default, False, which is exactly the value the defect produced on
      every path. That is why the switch-ON row is the one that catches it, and
      why the original pin -- run with the switch at its default OFF -- proved
      nothing.

    The feed's two columns carry DIFFERENT values, asserted before the request:
    with both equal no test can tell which column was read.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner, nsfw=False, nsfl=True)
    assert feed.nsfw is not feed.nsfl
    site = db.session.get(Site, 1)
    site.enable_nsfw = True
    site.enable_nsfl = site_nsfl
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get(f'/feed/{feed.id}/edit')

    assert response.status_code == 200
    form = captured['form']
    assert form.nsfw.data is False
    assert form.nsfl.data is expected_nsfl


# --------------------------------------------------------------------------
# Task 2: P2 -- NSFW/NSFL the site forbids.
# --------------------------------------------------------------------------


def _create_payload(app, client, **overrides):
    data = {'csrf_token': csrf(app, client), 'title': 'A feed', 'url': 'afeed',
            'description': '', 'communities': 'somecommunity@remote.example',
            'public': 'y'}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


@pytest.mark.parametrize('site_nsfw, site_nsfl', [
    (False, False),
    (True, True),
    (True, False),
])
def test_creating_a_feed_honours_the_sites_nsfw_switches(app, db_session,
                                                         site_nsfw, site_nsfl):
    """Was a PIN; INVERTED once the route enforced the site's switches.

    ORIGINAL PINNED CLAIM, now false: ":46-49 disable the two widgets when the
    site has NSFW or NSFL off. That is a browser-side hint -- nothing checks the
    submitted values, and make_feed writes them unconditionally." The same shape
    as D675 and D696, the third time this campaign has found
    `render_kw = {'disabled': True}` standing in for a server-side rule.

    Three rows, and the mixed one is the reason: a repair that cleared BOTH
    flags whenever either switch was off would pass a two-row test. The POST
    always submits both flags, so each row asserts which of them survived.

    form_communities_to_ids is patched because the form requires a communities
    value and a real one reaches search_for_community, which attempts a live
    webfinger.
    """
    instance, owner, stranger = _seed()
    site = db.session.get(Site, 1)
    site.enable_nsfw = site_nsfw
    site.enable_nsfl = site_nsfl
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            response = client.post('/feed/new', data=_create_payload(
                app, client, url='spicy', nsfw='y', nsfl='y'))

    assert response.status_code == 302
    made = Feed.query.filter_by(name='spicy').one()
    assert made.nsfw is site_nsfw
    assert made.nsfl is site_nsfl


# --------------------------------------------------------------------------
# Task 3: P3 -- ?topic_id= naming a topic that is not there.
# --------------------------------------------------------------------------


def test_creating_a_feed_from_a_missing_topic_is_a_404(app, db_session):
    """Was a PIN; INVERTED once the lookup became get_or_404.

    ORIGINAL PINNED CLAIM, now false: ":67-68 reads topic.communities with no
    check, so a topic id that resolves to nothing is a 500" --
    `AttributeError: 'NoneType' object has no attribute 'communities'`, asserted
    as an exception because the test client re-raises and no response is
    produced.

    A 404 is the right answer for a user-supplied query string whose link may
    have been opened before the topic was deleted, and it is what the rest of
    this file does for a missing feed.
    """
    instance, owner, stranger = _seed()
    assert db.session.get(Topic, 999) is None

    with app.test_client() as client:
        login(client, owner)
        response = client.get('/feed/new?topic_id=999')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# Task 4: P4 -- unsubscribing without telling the communities.
# --------------------------------------------------------------------------


def _subscribed_feed_with_community(owner, member, joined_via_feed=True,
                                    subscriptions_count=5, bystanders=True):
    """A feed the member subscribes to, carrying one REMOTE community the
    member joined through it.

    The community is remote (ap_id set, its own instance) because the shared
    leave_community federates an Undo Follow for exactly that case, and the
    point of P4 is that this route never does.
    """
    feed = _feed(owner, name='subscribedfeed')
    # Decoys first: Feed and Community have separate sequences, so without them
    # the feed and the community under test both land on id 1 -- which is
    # exactly why a mutant handing leave_community the FEED id survived this
    # file's first mutation pass (D653, fact 272).
    make_community(name='iddecoy1', host='remote.example')
    make_community(name='iddecoy2', host='remote.example')
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
    if bystanders:
        # A SECOND feed this member also subscribes to, and a community in the
        # feed the member never joined. The first keeps :647's feed_id filter
        # load-bearing; the second keeps :657's `membership and ...` operand
        # load-bearing, since without it the mutant that drops the operand finds
        # a row every time and never dereferences None.
        other = _feed(owner, name='anotherfeed')
        db.session.add(FeedMember(feed_id=other.id, user_id=member.id))
        never_joined = make_community(name='neverjoined', host='remote.example')
        db.session.add(FeedItem(feed_id=feed.id, community_id=never_joined.id))
    feed.subscriptions_count = 2
    member.feed_auto_leave = True
    db.session.commit()
    assert community.id != feed.id       # D653: these two travel together below
    return feed, community


def test_unsubscribing_by_get_is_refused(app, db_session):
    """Owner ruling, the D994 idiom: /feed/<name>/unsubscribe changed state on
    a GET, which login_required never CSRF-checks, so any page could make a
    signed-in user leave a feed. It is POST-only now, and a POST without the
    token is refused; the membership survives both."""
    instance, owner, member = _seed()
    feed, community = _subscribed_feed_with_community(owner, member)

    with app.test_client() as client:
        login(client, member)
        assert client.get(f'/feed/{feed.name}/unsubscribe').status_code == 405
        assert client.post(f'/feed/{feed.name}/unsubscribe').status_code == 400

    assert FeedMember.query.filter_by(user_id=member.id, feed_id=feed.id).count() == 1


def test_subscribing_by_get_is_refused(app, db_session):
    """Owner ruling, the same idiom: /feed/<name>/subscribe joined a feed on a
    GET, so any page could make a signed-in user join one. It is POST-only
    now, and a POST without the token is refused; no membership is made."""
    instance, owner, member = _seed()
    feed = _feed(owner, name='joinable')
    member.feed_auto_follow = False
    db.session.commit()

    with app.test_client() as client:
        login(client, member)
        assert client.get(f'/feed/{feed.name}/subscribe').status_code == 405
        assert client.post(f'/feed/{feed.name}/subscribe').status_code == 400

    assert FeedMember.query.filter_by(user_id=member.id, feed_id=feed.id).count() == 0


def test_every_feed_subscribe_control_is_a_form_carrying_the_token():
    """The templates that offered the GET link now post a form with the token."""
    import pathlib
    templates = pathlib.Path(__file__).resolve().parent.parent / 'app' / 'templates' / 'feed'
    for name in ('add_remote.html', 'lookup_remote.html', 'public_feeds.html',
                 '_feed_table_row.html', 'show_feed.html', '_feed_nav.html'):
        html = (templates / name).read_text()
        assert 'href="/feed/{{ new_feed.link() }}/subscribe"' not in html
        assert 'href="/feed/{{ feed.link() }}/subscribe"' not in html
        assert "href=\"{{ url_for('feed.subscribe'" not in html, name


def test_every_feed_unsubscribe_control_is_a_form_carrying_the_token():
    """The templates that offered the GET link now post a form with the token."""
    import pathlib
    templates = pathlib.Path(__file__).resolve().parent.parent / 'app' / 'templates' / 'feed'
    for name in ('add_remote.html', 'lookup_remote.html', 'public_feeds.html',
                 '_feed_table_row.html', 'show_feed.html', '_feed_nav.html'):
        html = (templates / name).read_text()
        assert 'href="/feed/{{ new_feed.link() }}/unsubscribe"' not in html
        assert 'href="/feed/{{ feed.link() }}/unsubscribe"' not in html
        assert "href=\"{{ url_for('feed.feed_unsubscribe'" not in html
        assert 'unsubscribe' in html and 'name="csrf_token"' in html, name


def test_unsubscribing_leaves_each_community_through_the_shared_function(app, db_session):
    """Was a PIN; INVERTED once the route called leave_community.

    ORIGINAL PINNED CLAIM, now false: ":640-641 deletes the CommunityMember row
    by hand", so the membership disappeared locally, the remote community was
    never told, and its subscriptions_count was left one too high --
    `send calls: 0` with the count still at its seeded 5.

    leave_community is asserted as DISPATCHED rather than by its effects: it
    goes through task_selector, whose delivery is another module's covered
    ground, and this test is about which function the route calls.
    """
    instance, owner, member = _seed()
    feed, community = _subscribed_feed_with_community(owner, member)

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.leave_community') as leave:
            response = client.post(f'/feed/{feed.name}/unsubscribe', data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert leave.call_count == 1
    assert leave.call_args.kwargs['community_id'] == community.id != feed.id
    assert FeedMember.query.filter_by(user_id=member.id, feed_id=feed.id).count() == 0
    # The member's OTHER subscription survives -- :647's delete names this feed.
    assert FeedMember.query.filter_by(user_id=member.id).count() == 1
    # And the feed's own counter goes DOWN, from the 2 the fixture seeded.
    assert db.session.get(Feed, feed.id).subscriptions_count == 1


def test_unsubscribing_never_drives_the_feeds_counter_negative(app, db_session):
    """D710, fixed: `subscriptions_count -= 1` had no floor, so a count that
    had drifted to 0 went to -1. It now stops at 0, as leave_feed's does."""
    instance, owner, member = _seed()
    feed, community = _subscribed_feed_with_community(owner, member)
    feed.subscriptions_count = 0
    db.session.commit()

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.leave_community'):
            client.post(f'/feed/{feed.name}/unsubscribe', data={'csrf_token': csrf(app, client)})

    assert FeedMember.query.filter_by(user_id=member.id, feed_id=feed.id).count() == 0
    assert db.session.get(Feed, feed.id).subscriptions_count == 0


def test_unsubscribing_leaves_alone_a_community_the_user_joined_themselves(app, db_session):
    """D673's guard, which the repaired route inherits: a community the user
    joined on their own -- joined_via_feed False -- is not left when the feed
    is.

    Without this control the repair is indistinguishable from one that leaves
    every community in the feed.
    """
    instance, owner, member = _seed()
    feed, community = _subscribed_feed_with_community(owner, member,
                                                      joined_via_feed=False)

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.leave_community') as leave:
            client.post(f'/feed/{feed.name}/unsubscribe', data={'csrf_token': csrf(app, client)})

    assert leave.call_count == 0
    assert CommunityMember.query.filter_by(user_id=member.id,
                                           community_id=community.id).count() == 1


def test_unsubscribing_with_auto_leave_off_keeps_every_community(app, db_session):
    """:653's False arm. The member IS in a community they joined through the
    feed, so the only thing keeping it is the preference -- without a real
    via-feed membership here, a mutant that ignored the preference would behave
    identically."""
    instance, owner, member = _seed()
    feed, community = _subscribed_feed_with_community(owner, member)
    member.feed_auto_leave = False
    db.session.commit()

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.leave_community') as leave:
            client.post(f'/feed/{feed.name}/unsubscribe', data={'csrf_token': csrf(app, client)})

    assert leave.call_count == 0
    assert CommunityMember.query.filter_by(user_id=member.id,
                                           community_id=community.id).count() == 1


def test_unsubscribing_busts_the_three_memoized_entries(app, db_session):
    """:674-676. CACHE_TYPE is NullCache in tests (D602, D589), so the effect is
    unobservable by construction and the CALLS are what is asserted -- by their
    first argument, since the route busts Feed.header_image elsewhere on other
    paths."""
    from app.utils import feed_membership, joined_communities, menu_subscribed_feeds
    instance, owner, member = _seed()
    feed, community = _subscribed_feed_with_community(owner, member)

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.leave_community'), \
                patch('app.feed.routes.cache.delete_memoized') as bust:
            client.post(f'/feed/{feed.name}/unsubscribe', data={'csrf_token': csrf(app, client)})

    busted = [call.args[0] for call in bust.call_args_list]
    assert feed_membership in busted
    assert menu_subscribed_feeds in busted
    assert joined_communities in busted


# --------------------------------------------------------------------------
# Task 5: the rest of feed_new and feed_edit.
# --------------------------------------------------------------------------


def test_a_banned_user_cannot_reach_the_create_form(app, db_session):
    """:43-44. show_ban_message runs before the form is even built, so a banned
    user never reaches feeds_for_form or the widget arms below it."""
    instance, owner, stranger = _seed()
    owner.banned = True
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.show_ban_message', return_value='banned') as ban, \
                patch('app.feed.routes.render_template') as render:
            response = client.get('/feed/new')

    assert response.status_code == 200
    assert ban.call_count == 1
    assert render.call_count == 0


def test_a_banned_user_cannot_reach_the_edit_form(app, db_session):
    """:143-144, feed_edit's twin of the arm above -- and it runs BEFORE the
    feed is loaded, so a banned user editing a feed that does not exist gets the
    ban message rather than a 404."""
    instance, owner, stranger = _seed()
    feed = _feed(owner)
    owner.banned = True
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.show_ban_message', return_value='banned') as ban:
            response = client.get(f'/feed/{feed.id}/edit')

    assert response.status_code == 200
    assert ban.call_count == 1


@pytest.mark.parametrize('is_admin, expect_disabled', [(False, True), (True, False)])
def test_the_create_form_disables_the_instance_feed_box_for_non_admins(
        app, db_session, is_admin, expect_disabled):
    """:50-51. The widget arm only -- D675 is the register entry for the fact
    that this constrains nothing on the server, and sub-project 50 repaired
    make_feed rather than the widget.

    feeds_for_form is asserted with its arguments in the same test: :52 passes
    (0, current_user.id), and the 0 is what tells make's form from edit's, which
    passes the feed id.
    """
    instance, owner, stranger = _seed()
    if is_admin:
        from app.models import Role, user_role
        role = Role(name='Admin', weight=0)
        db.session.add(role)
        db.session.commit()
        db.session.execute(user_role.insert().values(user_id=owner.id, role_id=role.id))
        db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.feeds_for_form', return_value=[]) as choices:
            client.get('/feed/new')

    form = captured['form']
    assert (form.is_instance_feed.render_kw == {'disabled': True}) is expect_disabled
    assert choices.call_args.args == (0, owner.id)


@pytest.mark.parametrize('site_nsfw, site_nsfl', [(False, False), (True, True)])
def test_the_create_form_disables_the_nsfw_boxes_the_site_forbids(app, db_session,
                                                                  site_nsfw, site_nsfl):
    """:46-49, both widget arms. Separate from the POST test above, which is
    about the server-side rule this round added: these two are the browser-side
    hint, and covering them says which is which."""
    instance, owner, stranger = _seed()
    site = db.session.get(Site, 1)
    site.enable_nsfw = site_nsfw
    site.enable_nsfl = site_nsfl
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            client.get('/feed/new')

    form = captured['form']
    assert (form.nsfw.render_kw == {'disabled': True}) is not site_nsfw
    assert (form.nsfl.render_kw == {'disabled': True}) is not site_nsfl


def test_creating_a_private_feed_appends_the_owner_to_its_url(app, db_session):
    """:58-60. A private feed's url becomes '<slug>/<owner>', which is what
    keeps two users' private feeds from colliding on Feed.name's unique index.

    The owner's name is not a substring of the slug, asserted live, so the
    composite is distinguishable from either half.
    """
    instance, owner, stranger = _seed()
    assert 'feedowner' not in 'secretfeed'

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()):
            response = client.post('/feed/new', data=_create_payload(
                app, client, url='secretfeed', public=None))

    assert response.status_code == 302
    assert Feed.query.filter_by(name='secretfeed/feedowner').count() == 1


def test_creating_a_feed_redirects_to_the_owners_feed_list(app, db_session):
    """:63-64, the success arm's flash and redirect. The Location is asserted in
    full: `back()` is not used here, so a regression that copied the redirect
    from a neighbouring route would send the user somewhere else entirely."""
    instance, owner, stranger = _seed()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.feed.routes.flash') as flash_stub:
            response = client.post('/feed/new', data=_create_payload(app, client))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/u/{owner.user_name}/myfeeds')
    assert flash_stub.call_count == 1


def test_creating_a_feed_from_a_topic_prefills_the_form(app, db_session):
    """:83-89, the topic pre-fill, which is the block P3's 404 now guards.

    Two communities, so the join is a join: the field is built by '\\n'.join
    over lemmy_link() with the leading '!' stripped, and a single community
    cannot show either the separator or the strip.
    """
    instance, owner, stranger = _seed()
    topic = Topic(name='Gardening', machine_name='gardening', num_communities=2)
    db.session.add(topic)
    db.session.commit()
    first = make_community(name='seeds', host='remote.example')
    second = make_community(name='soil', host='remote.example')
    first.topic_id = topic.id
    second.topic_id = topic.id
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get(f'/feed/new?topic_id={topic.id}')

    assert response.status_code == 200
    form = captured['form']
    assert form.title.data == 'Gardening'
    assert form.url.data == 'gardening'
    assert set(form.communities.data.split('\n')) == {
        first.lemmy_link().replace('!', ''), second.lemmy_link().replace('!', '')}
    assert '!' not in form.communities.data


def test_editing_someone_elses_feed_is_a_404(app, db_session):
    """:148-149. The route's ownership check refuses a stranger who is not an
    admin; an admin is admitted, as edit_feed admits one (D697)."""
    instance, owner, stranger = _seed()
    feed = _feed(owner)

    with app.test_client() as client:
        login(client, stranger)
        response = client.get(f'/feed/{feed.id}/edit')

    assert response.status_code == 404


def _admin(user):
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    assert user.is_admin()


def test_an_admin_may_edit_someone_elses_feed_on_the_web(app, db_session):
    """D697, fixed (owner ruling): admins may edit any feed on the web, as the
    API already allowed. The private feed keeps its OWNER's name suffix -- it
    is built from the feed's owner, not from whoever is editing."""
    instance, owner, stranger = _seed()
    _admin(stranger)
    feed = _feed(owner, name='lifecyclefeed/feedowner', public=False)

    with app.test_client() as client:
        login(client, stranger)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            assert client.get(f'/feed/{feed.id}/edit').status_code == 200
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.existing_communities', return_value=[]):
            response = client.post(f'/feed/{feed.id}/edit',
                                   data=_edit_payload(app, client, title='Admin edit',
                                                      url='lifecyclefeed/feedowner', public=None))

    assert response.status_code == 302
    edited = db.session.get(Feed, feed.id)
    assert edited.title == 'Admin edit'
    assert edited.name == 'lifecyclefeed/feedowner'


def test_editing_a_feed_that_is_not_there_is_a_404(app, db_session):
    """:147's get_or_404, reached before the ownership check."""
    instance, owner, stranger = _seed()
    with app.test_client() as client:
        login(client, owner)
        response = client.get('/feed/999/edit')
    assert response.status_code == 404


@pytest.mark.parametrize('subscriptions_count, expect_disabled', [(2, True), (1, False)])
def test_the_edit_form_disables_the_url_box_once_a_feed_has_subscribers(
        app, db_session, subscriptions_count, expect_disabled):
    """:157-158, the browser half of D696's rule; the server half is
    test_a_crafted_post_cannot_rename_a_feed_with_subscribers.

    Both rows are needed: with one, a mutant deleting the guard is invisible.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner)
    feed.subscriptions_count = subscriptions_count
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            client.get(f'/feed/{feed.id}/edit')

    form = captured['form']
    assert (form.url.render_kw == {'disabled': True}) is expect_disabled


def test_the_edit_form_prefills_every_field_from_the_feed(app, db_session):
    """:167-183's pre-fill block, the part P1 lives in.

    Seven fields are copied here and nothing asserted any of them before this
    round -- which is how the NSFL line came to read the NSFW column. Each value
    is distinct from the others so a mis-wired assignment cannot pass by
    coincidence, and feed_communities_for_edit is patched to a sentinel for the
    same reason.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner, nsfw=True, nsfl=False)
    parent = _feed(owner, name='parentfeed')
    feed.title = 'A distinctive title'
    feed.description = 'A distinctive description'
    feed.show_posts_in_children = True
    feed.parent_feed_id = parent.id
    feed.public = False
    feed.is_instance_feed = True
    site = db.session.get(Site, 1)
    site.enable_nsfw = site.enable_nsfl = True
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.feed_communities_for_edit',
                      return_value='!a@b') as communities:
            client.get(f'/feed/{feed.id}/edit')

    form = captured['form']
    assert form.title.data == 'A distinctive title'
    assert form.url.data == 'lifecyclefeed'
    assert form.description.data == 'A distinctive description'
    assert form.communities.data == '!a@b'
    assert communities.call_args.args == (feed.id,)
    assert form.show_child_posts.data is True
    assert form.parent_feed_id.data == parent.id != feed.id
    assert form.public.data is False
    assert form.is_instance_feed.data is True
    assert form.nsfw.data is True and form.nsfl.data is False


def _edit_payload(app, client, **overrides):
    data = {'csrf_token': csrf(app, client), 'title': 'Edited', 'url': 'lifecyclefeed',
            'description': '', 'communities': 'somecommunity@remote.example',
            'public': 'y'}
    data.update(overrides)
    return {k: v for k, v in data.items() if v is not None}


@pytest.mark.parametrize('new_url, referer, expected_location', [
    ('renamedfeed', 'https://test.piefed.local/f/lifecyclefeed', '/f/renamedfeed'),
    ('renamedfeed', 'https://test.piefed.local/elsewhere',
     'https://test.piefed.local/elsewhere'),
    ('lifecyclefeed', 'https://test.piefed.local/elsewhere',
     'https://test.piefed.local/elsewhere'),
    ('lifecyclefeed', 'https://test.piefed.local/f/lifecyclefeed',
     'https://test.piefed.local/f/lifecyclefeed'),
])
def test_saving_an_edit_redirects_by_whether_the_url_changed(app, db_session, new_url,
                                                             referer, expected_location):
    """:161-180. The POST arm: the slug rewrite, the url_changed/old_url
    bookkeeping, and the three redirect arms.

    Four rows, and the fourth exists because of the mutation pass: a rename
    whose referrer names the OLD url (the only case that cannot simply go back,
    because that page is gone), a rename from somewhere else, an edit that
    changed no url at all, and -- the one the first three missed -- an edit that
    changed no url whose referrer DOES name the feed. Without that row,
    hardcoding `url_changed = True` changes nothing observable, because the
    referrer test then fails anyway and both paths redirect to the referrer.
    The fourth row separates `/f/<name>` from the absolute referrer url.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner)

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.existing_communities', return_value=[]):
            response = client.post(f'/feed/{feed.id}/edit',
                                   data=_edit_payload(app, client, url=new_url),
                                   headers={'Referer': referer})

    assert response.status_code == 302
    assert response.headers['Location'] == expected_location
    assert db.session.get(Feed, feed.id).name == new_url


def test_renaming_a_feed_on_the_web_moves_its_activitypub_urls(app, db_session):
    """D711, fixed: the route rewrote `name` itself before calling edit_feed,
    so edit_feed saw no rename and skipped D1371's rewrite of the five actor
    urls -- on the web a renamed feed kept answering to its old identity. The
    route now leaves the slug to edit_feed and reads url_changed afterwards."""
    instance, owner, stranger = _seed()
    feed = _feed(owner)
    assert feed.is_local()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.existing_communities', return_value=[]):
            response = client.post(f'/feed/{feed.id}/edit',
                                   data=_edit_payload(app, client, url='renamedfeed'),
                                   headers={'Referer': 'https://test.piefed.local/f/lifecyclefeed'})

    assert response.headers['Location'] == '/f/renamedfeed'
    edited = db.session.get(Feed, feed.id)
    assert (edited.name, edited.machine_name) == ('renamedfeed', 'renamedfeed')
    assert edited.ap_profile_id == 'https://test.piefed.local/f/renamedfeed'
    assert edited.ap_outbox_url == 'https://test.piefed.local/f/renamedfeed/outbox'


def test_a_crafted_post_cannot_rename_a_feed_with_subscribers(app, db_session):
    """D696, fixed (owner ruling): the url box is disabled once a feed has
    subscribers, and the server now enforces it -- a crafted POST carrying a
    new url is refused and the feed keeps its name and actor urls."""
    instance, owner, stranger = _seed()
    feed = _feed(owner, subscriptions_count=2)

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.existing_communities', return_value=[]):
            response = client.post(f'/feed/{feed.id}/edit',
                                   data=_edit_payload(app, client, url='renamedfeed'))

    assert response.status_code == 400
    edited = db.session.get(Feed, feed.id)
    assert edited.name == 'lifecyclefeed'
    assert edited.ap_profile_id == 'https://test.piefed.local/f/lifecyclefeed'


# --------------------------------------------------------------------------
# Task 6: feed_delete's cache bust, feed_notification, feed_unsubscribe's tail.
# --------------------------------------------------------------------------
#
# THE TWO RESIDUALS THIS ROUND LEAVES, both with proofs and neither chased:
#
# :57 and the arc [56, 57] -- `form.url.data = form.url.data[3:]`, the '/f/'
#   prefix strip. apply_feed_url_rules (app/utils.py:4750-4762) rejects any url
#   containing a slash before validate_on_submit returns, on the public arm
#   (`^[a-zA-Z0-9_]+$`) and the private one (`^[a-zA-Z0-9_]+(?:/<owner>)?$`,
#   where the slash may only precede the owner's own name). Probed on both:
#   {'url': ['Feed urls can only contain letters, numbers, and underscores.']}.
#   Fact 75 CAUSE 5, unreachable data.
#
# The arc [646, 674] -- `if proceed:`. `proceed` is assigned True at :618 and
#   never reassigned, so the False arm cannot be taken. Fact 75 CAUSE 9, the
#   same tautology D669 registered in _feed_remove_community and D679 in
#   join_feed. Unlike join_feed's, this one DOES strand an arc, because the
#   condition has no second operand to reach its False outcome through.


@pytest.mark.parametrize('is_instance_feed', [True, False])
def test_deleting_an_instance_feed_busts_the_instance_menu(app, db_session,
                                                           is_instance_feed):
    """:216-218. The only part of feed_delete tests/test_redirect_back.py's
    TestFeedDeleteRedirect leaves uncovered.

    The bust is identified by its ARGUMENT rather than a call count, and the
    row that is not an instance feed is what keeps the guard load-bearing.

    Worth recording: :217 reads feed.is_instance_feed AFTER delete_feed has
    committed the deletion. That works -- the attribute was loaded before the
    delete and the instance is not refreshed -- and this test is what pins it,
    because a change to expire_on_commit anywhere would turn it into an
    ObjectDeletedError.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner, is_instance_feed=is_instance_feed)
    from app.utils import menu_instance_feeds

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.cache.delete_memoized') as bust:
            response = client.post(f'/feed/{feed.id}/delete',
                                   data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert db.session.get(Feed, feed.id) is None
    busted = [call.args[0] for call in bust.call_args_list]
    assert (menu_instance_feeds in busted) is is_instance_feed


def test_the_notification_toggle_creates_then_removes_a_subscription(app, db_session):
    """:334-349, both arms of the toggle, in one test because the second arm's
    precondition is the first arm's result.

    A second user's subscription to the same feed is the control for the
    filters: the lookup names entity_id, user_id AND type, and a delete that
    dropped the user_id filter would take the bystander's row.

    D709, fixed: this was a GET with a side effect and no CSRF token. It is a
    CSRF-checked POST now, so the toggle is driven by POST with a token.
    """
    from app.constants import NOTIF_FEED
    instance, owner, stranger = _seed()
    feed = _feed(owner)
    db.session.add(NotificationSubscription(name=feed.name, user_id=stranger.id,
                                            entity_id=feed.id, type=NOTIF_FEED))
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        token = csrf(app, client)
        with patch('app.feed.routes.render_template', return_value='toggled'):
            first = client.post(f'/feed/{feed.id}/notification', data={'csrf_token': token})
            created = NotificationSubscription.query.filter_by(
                user_id=owner.id, entity_id=feed.id, type=NOTIF_FEED).one()
            assert created.name == feed.name
            second = client.post(f'/feed/{feed.id}/notification', data={'csrf_token': token})

    assert first.status_code == 200 and second.status_code == 200
    assert NotificationSubscription.query.filter_by(
        user_id=owner.id, entity_id=feed.id, type=NOTIF_FEED).count() == 0
    assert NotificationSubscription.query.filter_by(
        user_id=stranger.id, entity_id=feed.id, type=NOTIF_FEED).count() == 1


def test_the_notification_toggle_404s_on_a_feed_that_is_not_there(app, db_session):
    """:336's get_or_404."""
    instance, owner, stranger = _seed()
    with app.test_client() as client:
        login(client, owner)
        response = client.post('/feed/999/notification', data={'csrf_token': csrf(app, client)})
    assert response.status_code == 404


def test_the_notification_toggle_by_get_or_without_a_token_is_refused(app, db_session):
    """D709, fixed: the toggle accepted GET, which login_required never
    CSRF-checks, so any page could flip a signed-in user's feed notifications.
    It is POST-only now, and a POST without a token is refused."""
    instance, owner, stranger = _seed()
    feed = _feed(owner)
    with app.test_client() as client:
        login(client, owner)
        assert client.get(f'/feed/{feed.id}/notification').status_code == 405
        assert client.post(f'/feed/{feed.id}/notification').status_code == 400
    assert NotificationSubscription.query.filter_by(user_id=owner.id, entity_id=feed.id).count() == 0


def test_the_notification_bell_is_a_form_carrying_the_token(app, db_session):
    """D709's template half: the bell posts a form with the token instead of
    linking, as the post and reply bells do since the D994 sibling fix."""
    instance, owner, stranger = _seed()
    feed = _feed(owner)
    with app.test_request_context('/'):
        login_user(owner)
        bell = render_template('feed/_notification_toggle.html', feed=feed)

    assert f'<form method="post" action="/feed/{feed.id}/notification"' in bell
    assert 'name="csrf_token"' in bell
    assert 'href=' not in bell


def test_saving_a_private_edit_appends_the_owner_to_the_url(app, db_session):
    """:163-164, the edit route's own private-url composite -- the twin of the
    create route's at :58-60, and asserted separately because the two have
    already diverged once: this one runs on a feed that may already carry the
    suffix, and :162's split('/')[0] is what stops it being appended twice.

    The feed starts as 'lifecyclefeed/feedowner' precisely to exercise that.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner, name='lifecyclefeed/feedowner', public=False)

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.existing_communities', return_value=[]):
            response = client.post(f'/feed/{feed.id}/edit',
                                   data=_edit_payload(app, client,
                                                      url='lifecyclefeed/feedowner',
                                                      public=None),
                                   headers={'Referer': 'https://test.piefed.local/x'})

    assert response.status_code == 302
    assert db.session.get(Feed, feed.id).name == 'lifecyclefeed/feedowner'


def _remote_feed_membership(member, domain='remote.example', name='remotefeed',
                            gone_forever=False):
    """A remote feed the member subscribes to, on an instance whose
    gone_forever answer is set deliberately."""
    remote_instance = make_instance(domain, software='piefed')
    remote_instance.gone_forever = gone_forever
    feed = Feed(user_id=None, title=name, name=name, machine_name=name,
                instance_id=remote_instance.id, public=True, subscriptions_count=3,
                ap_id=f'{name}@{domain}',
                ap_profile_id=f'https://{domain}/f/{name}',
                ap_public_url=f'https://{domain}/f/{name}',
                ap_inbox_url=f'https://{domain}/f/{name}/inbox')
    db.session.add(feed)
    db.session.commit()
    db.session.add(FeedMember(feed_id=feed.id, user_id=member.id))
    member.private_key = 'the-members-private-key'
    member.feed_auto_leave = False
    db.session.commit()
    return feed, remote_instance


def test_unsubscribing_from_a_remote_feed_sends_a_signed_undo(app, db_session):
    """:620-643. The Undo wrapping a Follow, delivered to the feed's inbox and
    signed with the LEAVING USER's credentials.

    args[2] and args[3] -- the private key and the key id -- are asserted here
    for the reason D663 gives: of three send_post_request call sites this
    campaign found at 100% coverage, exactly one asserted the signing identity.

    The Undo's own id and the Follow's are asserted DIFFERENT: they come from
    separate gibberish() calls under different path prefixes, so an Undo reusing
    the Follow's id is a distinguishable wrong answer.
    """
    instance, owner, member = _seed()
    feed, remote_instance = _remote_feed_membership(member)
    # A stored join request on an instance that is NOT ovo.st. D89, fixed
    # (owner ruling): its uuid, the original Follow's id, is reused for every
    # peer, where only ovo.st used to get it.
    stored = FeedJoinRequest(user_id=member.id, feed_id=feed.id)
    db.session.add(stored)
    db.session.commit()
    stored_uuid = stored.uuid

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.send_post_request') as send:
            response = client.post(f'/feed/{feed.ap_id}/unsubscribe', data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert send.call_count == 1
    assert send.call_args.args[1]['object']['id'].endswith(f'/activities/follow/{stored_uuid}')
    url, activity, private_key, key_id = send.call_args.args
    assert url == 'https://remote.example/f/remotefeed/inbox'
    assert private_key == 'the-members-private-key'
    assert key_id.endswith('#main-key')
    assert activity['type'] == 'Undo'
    assert activity['object']['type'] == 'Follow'
    assert '/activities/undo/' in activity['id']
    assert '/activities/follow/' in activity['object']['id']
    assert activity['id'] != activity['object']['id']
    assert FeedMember.query.filter_by(user_id=member.id, feed_id=feed.id).count() == 0


def test_unsubscribing_from_a_dead_remote_instance_sends_nothing(app, db_session):
    """:621's False arm. The membership still goes -- the local state is
    cleaned up whether or not the remote can be told, which is the same
    behaviour join_feed has when an instance is offline."""
    instance, owner, member = _seed()
    feed, remote_instance = _remote_feed_membership(member, gone_forever=True)

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.send_post_request') as send:
            response = client.post(f'/feed/{feed.ap_id}/unsubscribe', data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert send.call_count == 0
    assert FeedMember.query.filter_by(user_id=member.id, feed_id=feed.id).count() == 0


@pytest.mark.parametrize('with_join_request', [True, False])
def test_unsubscribing_reuses_the_stored_follow_id(app, db_session, with_join_request):
    """D89, fixed (owner ruling): this was ovo.st alone, singled out by
    domain. A peer that matches an Undo to the Follow by the id we first sent
    needs that id rather than a fresh one, so every peer now gets it whenever
    the row is still there. The two rows are the stored uuid and the
    fallback, and the uuid is read from the database before the call, because
    the row is deleted by the time the assertions run.
    """
    instance, owner, member = _seed()
    feed, remote_instance = _remote_feed_membership(member)
    stored_uuid = None
    if with_join_request:
        request_row = FeedJoinRequest(user_id=member.id, feed_id=feed.id)
        db.session.add(request_row)
        db.session.commit()
        stored_uuid = request_row.uuid

    with app.test_client() as client:
        login(client, member)
        with patch('app.feed.routes.send_post_request') as send:
            client.post(f'/feed/{feed.ap_id}/unsubscribe', data={'csrf_token': csrf(app, client)})

    follow_id = send.call_args.args[1]['object']['id']
    if with_join_request:
        assert follow_id.endswith(f'/activities/follow/{stored_uuid}')
    else:
        assert '/activities/follow/' in follow_id
        assert stored_uuid is None


def test_a_feed_owner_is_refused_and_keeps_their_membership(app, db_session):
    """:678-679. The owner arm: a flash, no deletion, and -- asserted here
    because nothing else would notice -- the subscriptions_count untouched."""
    instance, owner, stranger = _seed()
    feed = _feed(owner)
    db.session.add(FeedMember(feed_id=feed.id, user_id=owner.id, is_owner=True))
    feed.subscriptions_count = 7
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.flash') as flash_stub:
            response = client.post(f'/feed/{feed.name}/unsubscribe', data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert flash_stub.call_count == 1
    assert FeedMember.query.filter_by(user_id=owner.id, feed_id=feed.id).count() == 1
    assert db.session.get(Feed, feed.id).subscriptions_count == 7


def test_unsubscribing_from_a_feed_that_is_not_there_is_a_404(app, db_session):
    """:676-677's else arm -- actor_to_feed returning None."""
    instance, owner, stranger = _seed()
    with app.test_client() as client:
        login(client, stranger)
        response = client.post('/feed/nosuchfeed/unsubscribe', data={'csrf_token': csrf(app, client)})
    assert response.status_code == 404


def test_an_admin_sees_the_instance_feed_box_enabled_on_the_edit_form(app, db_session):
    """:154's False arm. The create route's twin is covered above; this one is
    separate because the two forms are different classes and the campaign has
    already watched a copy-pasted pair diverge inside this very function."""
    from app.models import Role, user_role
    instance, owner, stranger = _seed()
    feed = _feed(owner)
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=owner.id, role_id=role.id))
    db.session.commit()
    assert owner.is_admin()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            client.get(f'/feed/{feed.id}/edit')

    assert captured['form'].is_instance_feed.render_kw is None


def test_saving_an_edit_with_no_url_leaves_the_name_alone(app, db_session):
    """:161's False arm -- the case the route's own widget guard produces.

    A disabled input submits NOTHING, so the field is absent from the POST
    entirely and wtforms leaves `url.data` as None. That distinction is
    load-bearing twice over: EditFeedForm.validate:82 guards its
    required-field check with `if self.url.data is not None` for exactly this
    reason (its comment says so), and a test that posted an empty STRING
    instead gets 'Url is required.' and never reaches the route's
    branch. The first version of this test did that.

    url_changed stays False as well, which is why the redirect is the plain
    `back()` rather than the renamed-feed arm.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner, subscriptions_count=5)

    with app.test_client() as client:
        login(client, owner)
        with patch('app.shared.feed.form_communities_to_ids', return_value=set()), \
                patch('app.shared.feed.existing_communities', return_value=[]):
            response = client.post(f'/feed/{feed.id}/edit',
                                   data=_edit_payload(app, client, url=None),
                                   headers={'Referer': 'https://test.piefed.local/x'})

    assert response.status_code == 302
    assert response.headers['Location'] == 'https://test.piefed.local/x'
    assert db.session.get(Feed, feed.id).name == 'lifecyclefeed'


def test_unsubscribing_when_you_were_never_subscribed_does_nothing(app, db_session):
    """:616's False arm: feed_membership returns SUBSCRIPTION_NONMEMBER, which
    is falsy, so the route falls straight through to the redirect.

    The feed's subscriptions_count is asserted untouched -- without that, "no
    exception" is satisfied by a route that decremented it anyway.
    """
    instance, owner, stranger = _seed()
    feed = _feed(owner, subscriptions_count=4)

    with app.test_client() as client:
        login(client, stranger)
        response = client.post(f'/feed/{feed.name}/unsubscribe', data={'csrf_token': csrf(app, client)})

    assert response.status_code == 302
    assert db.session.get(Feed, feed.id).subscriptions_count == 4
    assert FeedMember.query.filter_by(feed_id=feed.id).count() == 0
