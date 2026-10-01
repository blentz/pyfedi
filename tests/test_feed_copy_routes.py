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

THE ROUND'S ONE RESIDUAL. feed_copy's '/f/' prefix strip -- the statement at
:239 and its arc [238, 239] -- is DEAD CODE, for the same reason D705 recorded
about feed_new's identical lines: this route uses the same AddCopyFeedForm,
whose validate() calls apply_feed_url_rules (app/utils.py:4750-4762), and that
rejects any url containing a slash on both the public and the private arm
before validate_on_submit returns. Fact 75 CAUSE 5, unreachable data, and the
second copy of it in this file -- which is itself the round's theme.
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
    site = db.session.get(Site, 1)
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
    site = db.session.get(Site, 1)
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

    D722, fixed: ap_profile_id alone was lowercased. All five now come from one
    base, as make_feed builds them. The split was latent -- the route lowercases
    the url first -- so this row held before the fix too and pins the shape.
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


# --------------------------------------------------------------------------
# Task 2: the rest of feed_copy.
# --------------------------------------------------------------------------


def test_a_banned_user_cannot_copy_a_feed(app, db_session):
    """:227-228, and it runs before the feed is loaded, so a banned user copying
    a feed that does not exist gets the ban message rather than a 404."""
    instance, owner = _seed()
    source = _feed(owner)
    owner.banned = True
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.show_ban_message', return_value='banned') as ban:
            response = client.get(f'/feed/{source.id}/copy')

    assert response.status_code == 200
    assert ban.call_count == 1


def test_copying_a_feed_that_is_not_there_is_a_404(app, db_session):
    """:230's get_or_404."""
    instance, owner = _seed()
    with app.test_client() as client:
        login(client, owner)
        response = client.get('/feed/999/copy')
    assert response.status_code == 404


@pytest.mark.parametrize('is_admin, expect_disabled', [(False, True), (True, False)])
def test_the_copy_form_disables_the_instance_feed_box_for_non_admins(
        app, db_session, is_admin, expect_disabled):
    """:234-235's widget arm, which D675 established constrains nothing on the
    server -- the check this round added at :237 is what does. Covered so the
    two are visibly separate things."""
    instance, owner = _seed()
    source = _feed(owner)
    if is_admin:
        _make_admin(owner)

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.feeds_for_form', return_value=[]) as choices:
            client.get(f'/feed/{source.id}/copy')

    assert (captured['form'].is_instance_feed.render_kw == {'disabled': True}) is expect_disabled
    assert choices.call_args.args == (0, owner.id)


@pytest.mark.parametrize('public, expected_name', [
    (True, 'privatecopy'),
    (False, 'privatecopy/feedowner'),
])
def test_copying_a_feed_privately_appends_the_owner_exactly_once(app, db_session, public,
                                                                 expected_name):
    """Was a PIN; INVERTED once the route split the url before re-appending.

    ORIGINAL PINNED CLAIM, now false: a privately copied feed got the owner
    suffix TWICE --

    apply_feed_url_rules (app/utils.py:4744-4745) already rewrites a private
    feed's url to '<slug>/<owner>' during form validation. :242-244 then
    slugifies that whole string -- turning the '/' into '_' -- and appends the
    owner again, so 'privatecopy' becomes 'privatecopy_feedowner/feedowner'.

    feed_new does not do this: its private arm slugifies
    `form.url.data.strip().split('/')[0]` first (:57), which drops the suffix
    the validator added before re-appending it. feed_copy skips the split.

    The name is the feed's ActivityPub identity as well as its url, so the
    owner got an actor at /f/privatecopy_feedowner/feedowner.

    The public row is the control: the split must not disturb a public feed's
    url, and the owner's name is not a substring of the slug, so the composite
    is distinguishable from either half.
    """
    instance, owner = _seed()
    source = _feed(owner)
    assert 'feedowner' not in 'privatecopy'

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            response = client.post(f'/feed/{source.id}/copy',
                                   data=_copy_payload(app, client, url='privatecopy',
                                                      public='y' if public else None))

    assert response.status_code == 302
    assert Feed.query.filter_by(name=expected_name).count() == 1


@pytest.mark.parametrize('with_parent', [True, False, 'zero'])
def test_copying_a_feed_sets_its_parent_only_when_one_is_given(app, db_session, with_parent):
    """:264-267, whose else arm assigns None explicitly.

    The 'zero' row makes that explicit assignment observable: 0 is falsy, so it
    takes the else arm, and an implementation that stored the given value there
    would write 0 rather than None.
    """
    instance, owner = _seed()
    source = _feed(owner)
    parent = _feed(owner, name='parentfeed')

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.feeds_for_form',
                   return_value=[(parent.id, parent.title), (0, 'none')]), \
                patch('app.feed.routes.render_template', return_value='rendered'):
            given = {True: str(parent.id), 'zero': '0', False: None}[with_parent]
            response = client.post(
                f'/feed/{source.id}/copy',
                data=_copy_payload(app, client, parent_feed_id=given))

    assert response.status_code == 302
    made = Feed.query.filter_by(name='copiedfeed').one()
    if with_parent is True:
        assert made.parent_feed_id == parent.id != made.id
    else:
        assert made.parent_feed_id is None


@pytest.mark.parametrize('saved', [True, False])
def test_copying_a_feed_attaches_an_uploaded_icon_only_when_it_saves(app, db_session, saved):
    """:268-277. Two guards per file: a filename must be present, and
    save_icon_file must return something.

    The False row is the one a single-state test drops, and it is not
    hypothetical -- save_icon_file returns None for a file it will not accept.
    The banner arm is asserted in the same test because the two blocks are
    copies of each other and a divergence between them is what this campaign
    keeps finding.
    """
    from app.models import File
    instance, owner = _seed()
    source = _feed(owner)
    icon = File(source_url='https://example.test/icon.png')
    banner = File(source_url='https://example.test/banner.png')
    db.session.add_all([icon, banner])
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.save_icon_file',
                      return_value=icon if saved else None) as save_icon, \
                patch('app.feed.routes.save_banner_file',
                      return_value=banner if saved else None) as save_banner:
            response = client.post(f'/feed/{source.id}/copy', data=_copy_payload(
                app, client,
                icon_file=(io.BytesIO(b'icon-bytes'), 'icon.png'),
                banner_file=(io.BytesIO(b'banner-bytes'), 'banner.png')))

    assert response.status_code == 302
    made = Feed.query.filter_by(name='copiedfeed').one()
    assert save_icon.call_count == 1 and save_banner.call_count == 1
    assert save_icon.call_args.kwargs == {'directory': 'feeds'}
    assert (made.icon_id == icon.id) is saved
    assert (made.image_id == banner.id) is saved


def test_copying_a_feed_with_empty_file_parts_saves_nothing(app, db_session):
    """:290 and :295's empty-filename case -- the ORDINARY one, since a browser
    posts both parts whether or not the user picked anything.

    AND THE FILENAME OPERAND IS PROVABLY REDUNDANT, which the mutation pass
    found and this docstring records rather than pretending a test could kill
    it: `werkzeug.datastructures.FileStorage.__bool__` returns
    `bool(self.filename)`, verified in the container --

        def __bool__(self) -> bool:
            return bool(self.filename)

        FileStorage(filename='')      -> False
        FileStorage(filename='x.png') -> True

    -- so `icon_file and icon_file.filename != ''` has a second operand that
    can never change the answer. Dropping it is an equivalent mutant, fact 75
    cause 6, at both file blocks.
    """
    instance, owner = _seed()
    source = _feed(owner)

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.save_icon_file') as save_icon, \
                patch('app.feed.routes.save_banner_file') as save_banner:
            client.post(f'/feed/{source.id}/copy', data=_copy_payload(app, client))

    assert save_icon.call_count == 0
    assert save_banner.call_count == 0
    made = Feed.query.filter_by(name='copiedfeed').one()
    assert made.icon_id is None and made.image_id is None


def test_copying_a_feed_brings_its_communities_and_counts_them(app, db_session):
    """:283-287 and :301. The bystander feed's item must NOT come across, which
    is what keeps the query's feed_to_copy.id load-bearing, and
    num_communities is asserted against the number copied rather than against
    a constant."""
    instance, owner = _seed()
    source = _feed(owner)
    bystander = _feed(owner, name='bystanderfeed')
    first = make_community(name='alpha', host='remote.example')
    second = make_community(name='beta', host='remote.example')
    elsewhere = make_community(name='gamma', host='remote.example')
    db.session.add_all([FeedItem(feed_id=source.id, community_id=first.id),
                        FeedItem(feed_id=source.id, community_id=second.id),
                        FeedItem(feed_id=bystander.id, community_id=elsewhere.id)])
    owner.feed_auto_follow = False
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'):
            client.post(f'/feed/{source.id}/copy', data=_copy_payload(app, client))

    made = Feed.query.filter_by(name='copiedfeed').one()
    copied = FeedItem.query.filter_by(feed_id=made.id).all()
    assert {item.community_id for item in copied} == {first.id, second.id}
    assert len(copied) == 2
    assert made.num_communities == 2
    assert FeedItem.query.filter_by(feed_id=bystander.id).count() == 1


@pytest.mark.parametrize('auto_follow, already_member, expect_subscribe', [
    (True, False, True),
    (True, True, False),
    (False, False, False),
])
def test_copying_a_feed_subscribes_to_communities_the_user_is_not_in(
        app, db_session, auto_follow, already_member, expect_subscribe):
    """:290-299, both operands of `item.community_id not in member_of_ids and
    current_user.feed_auto_follow` isolated.

    do_subscribe is patched at app.community.routes, where :296's deferred
    import resolves it, and the actor is asserted: :298 chooses between ap_id
    and name, and the community here has an ap_id that differs from its name.

    D719, fixed: this call was synchronous, so copying a feed ran every
    subscribe inside the request. Like join_feed it now dispatches, and runs
    inline only under current_app.debug (False here).
    """
    instance, owner = _seed()
    source = _feed(owner)
    community = make_community(name='alpha', host='remote.example')
    community.ap_id = 'alpha@remote.example'
    db.session.add(FeedItem(feed_id=source.id, community_id=community.id))
    # ANOTHER user's membership of the same community. :312's probe filters by
    # user_id; without this row that filter is free, and a mutant dropping it
    # concludes the copier is already a member and skips the subscribe.
    bystander = make_user(instance, 'someoneelse', local=True)
    db.session.add(CommunityMember(user_id=bystander.id, community_id=community.id))
    if already_member:
        db.session.add(CommunityMember(user_id=owner.id, community_id=community.id))
    owner.feed_auto_follow = auto_follow
    db.session.commit()

    subscribe_target = 'app.community.routes.do_subscribe'
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch(subscribe_target) as subscribe:
            client.post(f'/feed/{source.id}/copy', data=_copy_payload(app, client))

    assert app.debug is False
    assert subscribe.call_count == 0
    assert subscribe.delay.call_count == (1 if expect_subscribe else 0)
    if expect_subscribe:
        assert subscribe.delay.call_args.args == ('alpha@remote.example', owner.id)
        assert subscribe.delay.call_args.kwargs == {'joined_via_feed': True}


def test_copying_a_feed_makes_the_copier_its_owner_and_redirects(app, db_session):
    """:305-310. is_owner is asserted explicitly -- it is what feed_unsubscribe
    reads to refuse the owner -- and the redirect is asserted in full, because
    it differs from feed_new's (that one goes to the owner's feed list; this one
    goes to the index, registered as a divergence)."""
    instance, owner = _seed()
    source = _feed(owner)

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.flash') as flash_stub:
            response = client.post(f'/feed/{source.id}/copy', data=_copy_payload(app, client))

    made = Feed.query.filter_by(name='copiedfeed').one()
    # The title comes from the form's title field, not its url -- they carry
    # different values here for exactly that reason.
    assert made.title == 'Copied' != made.name
    membership = FeedMember.query.filter_by(feed_id=made.id, user_id=owner.id).one()
    assert membership.is_owner is True
    assert flash_stub.call_count == 1
    # main.index is '/home' in this app, not '/'. Asserted as the resolved url
    # rather than a literal guess, and asserted at all because it DIFFERS from
    # feed_new's redirect (the owner's feed list) -- a divergence between two
    # routes that do the same job, registered rather than resolved.
    with app.test_request_context():
        from flask import url_for
        assert response.headers['Location'] == url_for('main.index')


def test_the_copy_form_prefills_every_field_from_the_source_feed(app, db_session):
    """:313-327, the GET pre-fill -- the block P3's slip lives in. Every field
    gets a distinctive value so a mis-wired assignment cannot pass by
    coincidence."""
    instance, owner = _seed()
    source = _feed(owner, nsfw=True, nsfl=False, public=False)
    source.title = 'A distinctive title'
    source.description = 'A distinctive description'
    source.show_posts_in_children = True
    source.is_instance_feed = True
    site = db.session.get(Site, 1)
    site.enable_nsfw = site.enable_nsfl = True
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.feed_communities_for_edit',
                      return_value='!a@b') as communities:
            client.get(f'/feed/{source.id}/copy')

    form = captured['form']
    assert form.title.data == 'A distinctive title'
    assert form.url.data == 'sourcefeed'
    assert form.description.data == 'A distinctive description'
    assert form.communities.data == '!a@b'
    assert communities.call_args.args == (source.id,)
    assert form.show_child_posts.data is True
    assert form.public.data is False
    assert form.is_instance_feed.data is True
    assert form.nsfw.data is True and form.nsfl.data is False


@pytest.mark.parametrize('site_nsfw', [True, False])
def test_the_copy_form_disables_the_nsfw_box_the_site_forbids(app, db_session, site_nsfw):
    """:318-321's widget arm, the twin of :322-325's NSFL one."""
    instance, owner = _seed()
    source = _feed(owner)
    site = db.session.get(Site, 1)
    site.enable_nsfw = site_nsfw
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            client.get(f'/feed/{source.id}/copy')

    assert (captured['form'].nsfw.render_kw == {'disabled': True}) is not site_nsfw


# --------------------------------------------------------------------------
# Task 3: feed_add_remote.
# --------------------------------------------------------------------------


def _remote_feed(name='remotefeed', domain='remote.example', banned=False):
    remote_instance = make_instance(domain, software='piefed')
    feed = Feed(title=name, name=name, machine_name=name, instance_id=remote_instance.id,
                public=True, banned=banned, ap_id=f'{name}@{domain}',
                ap_profile_id=f'https://{domain}/f/{name}',
                ap_public_url=f'https://{domain}/f/{name}')
    db.session.add(feed)
    db.session.commit()
    return feed


@pytest.mark.parametrize('address, expected_lookup', [
    ('~remotefeed@remote.example', '~remotefeed@remote.example'),
    ('remotefeed@remote.example', '~remotefeed@remote.example'),
    ('https://remote.example/f/remotefeed', '~remotefeed@remote.example'),
    # MIXED CASE, which is what makes :103's .lower() load-bearing: without it
    # the lookup string keeps the capitals and finds nothing.
    ('~RemoteFeed@Remote.Example', '~remotefeed@remote.example'),
])
def test_searching_for_a_remote_feed_normalises_every_address_shape(app, db_session,
                                                                    address,
                                                                    expected_lookup):
    """:105-118's four-way fork, three of whose arms reach search_for_feed.

    The assertion is what search_for_feed was HANDED, because that is the only
    observable difference between the arms: all three end at the same lookup
    string by different routes -- the '~' form passes it through, the bare
    'name@host' form prefixes it, and the url form goes through
    extract_domain_and_actor first.
    """
    instance, owner = _seed()
    found = _remote_feed()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.search_for_feed', return_value=found) as search:
            response = client.post('/feed/add_remote', data={
                'csrf_token': csrf(app, client), 'address': address})

    assert response.status_code == 200
    assert search.call_args.args == (expected_lookup,)
    assert captured['new_feed'] is found


def test_a_tilde_address_without_a_host_is_not_a_search(app, db_session):
    """:105's second operand. '~name' with no '@' is not an address: the arm
    requires BOTH, and without this row dropping the '@' test changes nothing,
    because every other address in this file carries one."""
    instance, owner = _seed()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.search_for_feed') as search, \
                patch('app.feed.routes.flash') as flash_stub:
            client.post('/feed/add_remote', data={
                'csrf_token': csrf(app, client), 'address': '~remotefeed'})

    assert search.call_count == 0
    assert flash_stub.call_count == 2


def test_searching_for_a_person_does_nothing_yet(app, db_session):
    """:111-113, the `...` branch: an @person@host address is recognised and
    then deliberately ignored, so no search happens and the not-found flash
    fires. Covered because a statement that does nothing is still a statement,
    and because a later round adding person search will want the pin."""
    instance, owner = _seed()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.search_for_feed') as search, \
                patch('app.feed.routes.flash') as flash_stub:
            client.post('/feed/add_remote', data={
                'csrf_token': csrf(app, client), 'address': '@someone@remote.example'})

    assert search.call_count == 0
    assert captured['new_feed'] is None
    assert flash_stub.call_count == 1


def test_an_unrecognised_address_gets_the_format_help(app, db_session):
    """:119-122's else arm, and then :123's not-found flash on top of it -- two
    flashes, which is what distinguishes this arm from the ones that search."""
    instance, owner = _seed()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.search_for_feed') as search, \
                patch('app.feed.routes.flash') as flash_stub:
            client.post('/feed/add_remote', data={
                'csrf_token': csrf(app, client), 'address': 'justsomewords'})

    assert search.call_count == 0
    assert flash_stub.call_count == 2


@pytest.mark.parametrize('message, expect_flash', [
    ('remote.example is blocked.', 2),
    ('the remote server exploded', 1),
])
def test_a_failed_search_flashes_once_or_twice(app, db_session, message, expect_flash):
    """:106-110's except arm.

    REGISTERED, NOT FIXED: an exception whose message does not contain
    'is blocked.' is caught and then dropped -- not re-raised, not logged, not
    shown -- and the user is told 'Feed not found.' The two rows are the two
    outcomes: the blocked message adds its own flash on top of the not-found
    one, and anything else is silent.
    """
    instance, owner = _seed()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.search_for_feed', side_effect=Exception(message)), \
                patch('app.feed.routes.flash') as flash_stub:
            response = client.post('/feed/add_remote', data={
                'csrf_token': csrf(app, client), 'address': '~remotefeed@remote.example'})

    assert response.status_code == 200
    assert flash_stub.call_count == expect_flash


@pytest.mark.parametrize('enable_nsfw', [True, False])
def test_the_not_found_message_mentions_nsfw_only_when_the_site_blocks_it(app, db_session,
                                                                          enable_nsfw):
    """:123-128. The two messages differ, and which one fires depends on the
    site's NSFW switch -- a feed that exists remotely but is NSFW is invisible
    to a site with NSFW off, so the second message explains that."""
    instance, owner = _seed()
    site = db.session.get(Site, 1)
    site.enable_nsfw = enable_nsfw
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.search_for_feed', return_value=None), \
                patch('app.feed.routes.flash') as flash_stub:
            client.post('/feed/add_remote', data={
                'csrf_token': csrf(app, client), 'address': '~remotefeed@remote.example'})

    message = str(flash_stub.call_args.args[0])
    assert ('nsfw' in message.lower()) is not enable_nsfw


def test_finding_a_feed_busts_its_membership_cache_and_reports_subscription(app, db_session):
    """:129-130 and the render's `subscribed` argument at :134.

    The member row is what makes `subscribed` True rather than the default, and
    the bust is asserted as a CALL because CACHE_TYPE is NullCache (D602).
    """
    from app.utils import feed_membership as feed_membership_fn
    instance, owner = _seed()
    found = _remote_feed()
    db.session.add(FeedMember(feed_id=found.id, user_id=owner.id))
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.search_for_feed', return_value=found), \
                patch('app.feed.routes.cache.delete_memoized') as bust:
            client.post('/feed/add_remote', data={
                'csrf_token': csrf(app, client), 'address': '~remotefeed@remote.example'})

    # By membership, not by an exact list: something else in the request also
    # busts get_setting, so a list comparison measures that instead.
    assert feed_membership_fn in [call.args[0] for call in bust.call_args_list]
    assert captured['subscribed'] is True


def test_the_add_remote_page_renders_for_a_get_with_nothing_found(app, db_session):
    """The GET arm, and the reason it does not raise: feed_membership(user,
    None) returns False (app/utils.py:1677-1678), and `False >=
    SUBSCRIPTION_MEMBER` is a legal comparison that answers False. Asserted
    here rather than discovered by a later reader."""
    from app.utils import feed_membership as feed_membership_fn
    instance, owner = _seed()
    assert feed_membership_fn(owner, None) is False
    assert (feed_membership_fn(owner, None) >= SUBSCRIPTION_MEMBER) is False

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render):
            response = client.get('/feed/add_remote')

    assert response.status_code == 200
    assert captured['new_feed'] is None
    assert captured['subscribed'] is False


def test_a_banned_user_cannot_search_for_remote_feeds(app, db_session):
    """:98-99."""
    instance, owner = _seed()
    owner.banned = True
    db.session.commit()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.show_ban_message', return_value='banned') as ban:
            response = client.get('/feed/add_remote')

    assert response.status_code == 200
    assert ban.call_count == 1


# --------------------------------------------------------------------------
# Task 4: lookup.
# --------------------------------------------------------------------------


def test_looking_up_a_local_feed_redirects_to_it(app, db_session):
    """:689-690. The local shortcut, taken before any lookup: the domain in the
    url is this server's, so there is nothing remote to search for."""
    instance, owner = _seed()
    server = app.config['SERVER_NAME']

    with app.test_client() as client:
        response = client.get(f'/feed/lookup/localfeed/{server}')

    assert response.status_code == 302
    assert response.headers['Location'] == '/f/localfeed'


def test_looking_up_a_known_remote_feed_redirects_to_it(app, db_session):
    """:692-697. The ap_id lookup, and the lower-casing that precedes it: the
    request uses MIXED case, and without :692-693 the filter_by would miss the
    row and fall through to a remote search."""
    instance, owner = _seed()
    found = _remote_feed()

    with app.test_client() as client:
        with patch('app.feed.routes.search_for_feed') as search:
            response = client.get('/feed/lookup/RemoteFeed/Remote.Example')

    assert search.call_count == 0
    assert response.status_code == 302
    assert response.headers['Location'] == '/f/remotefeed@remote.example'


def test_looking_up_an_unknown_feed_anonymously_asks_for_a_login(app, db_session):
    """:721-724. The anonymous arm: a flash and back('/'), with no search at
    all -- searching is what costs an outbound request, which is why it is
    behind a login."""
    _seed()

    with app.test_client() as client:
        with patch('app.feed.routes.search_for_feed') as search, \
                patch('app.feed.routes.flash') as flash_stub:
            response = client.get('/feed/lookup/unknown/remote.example')

    assert search.call_count == 0
    assert flash_stub.call_count == 1
    assert response.status_code == 302


@pytest.mark.parametrize('enable_nsfw', [True, False])
def test_looking_up_an_unknown_feed_while_logged_in_searches_for_it(app, db_session,
                                                                    enable_nsfw):
    """:698-713. The authenticated arm, its search, and the two not-found
    messages -- the same pair feed_add_remote carries, in a second copy."""
    instance, owner = _seed()
    site = db.session.get(Site, 1)
    site.enable_nsfw = enable_nsfw
    db.session.commit()

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.search_for_feed', return_value=None) as search, \
                patch('app.feed.routes.flash') as flash_stub:
            response = client.get('/feed/lookup/unknown/remote.example')

    assert response.status_code == 200
    assert search.call_args.args == ('~unknown@remote.example',)
    assert captured['new_feed'] is None
    assert captured['subscribed'] is False
    message = str(flash_stub.call_args.args[0])
    assert ('nsfw' in message.lower()) is not enable_nsfw


@pytest.mark.parametrize('banned', [True, False])
def test_looking_up_a_feed_warns_when_it_is_banned_here(app, db_session, banned):
    """:714-716. A found feed that this instance has banned still renders, with
    a warning -- the row is the difference between 'here is the feed' and 'here
    is the feed, and you cannot have it'."""
    instance, owner = _seed()
    # One remote feed, minted once: _remote_feed also mints its instance, and
    # Instance.domain is unique, so a second call for the same domain is an
    # IntegrityError rather than a second fixture.
    fresh = _remote_feed(name='newlyfound', banned=banned)

    captured, fake_render = _capture_form()
    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', side_effect=fake_render), \
                patch('app.feed.routes.search_for_feed', return_value=fresh), \
                patch('app.feed.routes.flash') as flash_stub:
            client.get('/feed/lookup/unknown/remote.example')

    assert captured['new_feed'] is fresh
    assert flash_stub.call_count == (1 if banned else 0)


def test_looking_up_a_blocked_instance_says_so(app, db_session):
    """:705-707, lookup's copy of feed_add_remote's except arm -- and R4's
    silent half lives here too: a message without 'is blocked.' is dropped."""
    instance, owner = _seed()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.search_for_feed',
                      side_effect=Exception('remote.example is blocked.')), \
                patch('app.feed.routes.flash') as flash_stub:
            response = client.get('/feed/lookup/unknown/remote.example')

    assert response.status_code == 200
    assert flash_stub.call_count == 2


def test_a_lookup_search_that_fails_for_another_reason_says_nothing(app, db_session):
    """:729's False arm -- lookup's half of R4, and the twin of the
    add_remote test above.

    An exception whose message does not contain 'is blocked.' is caught and
    dropped: not re-raised, not logged, not shown. The user gets 'Feed not
    found.' and nothing else, so a remote server that is timing out and one
    that genuinely has no such feed are indistinguishable. Pinned in BOTH
    copies, because that is what stops one of them being repaired while the
    other is forgotten -- which is this round's whole theme.
    """
    instance, owner = _seed()

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.search_for_feed',
                      side_effect=Exception('the remote server exploded')), \
                patch('app.feed.routes.flash') as flash_stub:
            response = client.get('/feed/lookup/unknown/remote.example')

    assert response.status_code == 200
    assert flash_stub.call_count == 1
    assert 'not found' in str(flash_stub.call_args.args[0]).lower()


def test_copying_a_feed_to_a_url_that_is_taken_is_refused_before_the_insert(app, db_session):
    """D681's sibling, fixed. feed_copy builds its Feed itself rather than
    through make_feed, so the form's normalised-name check was its only guard
    and anything past it was an IntegrityError at the unique index. The route
    now refuses a taken name before the insert, as make_feed does. The form's
    check is bypassed here so that the route's own is what answers."""
    instance, owner = _seed()
    source = _feed(owner)
    _feed(owner, name='copiedfeed')

    with app.test_client() as client:
        login(client, owner)
        with patch('app.feed.routes.AddCopyFeedForm.validate',
                   new=lambda self, extra_validators=None: True), \
                patch('app.feed.routes.render_template', return_value='rendered'), \
                patch('app.feed.routes.flash') as flash_stub:
            response = client.post(f'/feed/{source.id}/copy', data=_copy_payload(app, client))

    assert response.status_code == 302
    assert response.headers['Location'].endswith(f'/feed/{source.id}/copy')
    assert 'A Feed with this url already exists.' in str(flash_stub.call_args)
    assert Feed.query.filter_by(name='copiedfeed').count() == 1
