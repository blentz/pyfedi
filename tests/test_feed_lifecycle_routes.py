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
    site = Site.query.get(1)
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
    site = Site.query.get(1)
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
    assert Topic.query.get(999) is None

    with app.test_client() as client:
        login(client, owner)
        response = client.get('/feed/new?topic_id=999')

    assert response.status_code == 404


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
            response = client.get(f'/feed/{feed.name}/unsubscribe')

    assert response.status_code == 302
    assert leave.call_count == 1
    assert leave.call_args.kwargs['community_id'] == community.id
    assert FeedMember.query.filter_by(user_id=member.id, feed_id=feed.id).count() == 0


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
            client.get(f'/feed/{feed.name}/unsubscribe')

    assert leave.call_count == 0
    assert CommunityMember.query.filter_by(user_id=member.id,
                                           community_id=community.id).count() == 1


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
    site = Site.query.get(1)
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
    """:148-149. The route's own ownership check, which is stricter than
    edit_feed's: the shared function admits an admin (D697), this route does
    not, and that divergence is registered rather than resolved."""
    instance, owner, stranger = _seed()
    feed = _feed(owner)

    with app.test_client() as client:
        login(client, stranger)
        response = client.get(f'/feed/{feed.id}/edit')

    assert response.status_code == 404


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
    """:157-158, and D696's register entry is the reason this is worth a test:
    the guard exists only in the browser. A crafted POST renames a feed with
    subscribers anyway, and D695 then leaves its actor url on the old name.

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
    site = Site.query.get(1)
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
])
def test_saving_an_edit_redirects_by_whether_the_url_changed(app, db_session, new_url,
                                                             referer, expected_location):
    """:161-180. The POST arm: the slug rewrite, the url_changed/old_url
    bookkeeping, and the three redirect arms.

    The rows are the three outcomes: a rename whose referrer names the OLD url
    (the only case that cannot simply go back, because that page is gone), a
    rename from somewhere else, and an edit that changed no url at all. The
    first row is the one that distinguishes :175's `referrer().endswith(old_url)`
    from the plain `back()` the other two take.
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
    assert Feed.query.get(feed.id).name == new_url
