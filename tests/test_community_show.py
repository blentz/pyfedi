"""`show_community` -- the page a browser reaches at /c/<name>.

Sub-project 80, slice F. Four defects, all measured:

* the comments view listed the discussion under posts the posts view refuses
  to show -- a post a moderator had removed, and a post still awaiting review
  (D1005). `Community.replies` is every PostReply in the community with no
  join to Post at all;
* the breadcrumb walk up the topic tree read `.parent_id` off a `None` row
  when a parent topic had been deleted, and would walk forever on a cycle;
  same walk, same two faults, over feeds (D1006);
* `community.instance.gone_forever` on a remote community with no instance
  row, which `Community.instance_id` being nullable makes reachable (D1007).

Two things about the fixtures, both of which cost a run to find:

* `Site.private_instance` defaults to **True** (`app/models.py:4014`), so an
  anonymous request to any page is redirected to the login form before the
  route runs. Every row here that is about an anonymous visitor turns it off.
* the page is reached through `activitypub.community_profile`, which looks a
  local community up by `ap_profile_id` **and** `ap_id=None`, and a remote one
  by `ap_id` alone. A remote community therefore has to be visited at
  `/c/<name>@<host>`, not through `url_for(... actor=community.name)`.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import (Community, CommunityBan, CommunityFlair,
                        CommunityFlairBlock, Feed, FeedItem, Instance,
                        Language, Post, Site, Topic, User, UserFlair)
from app.utils import utcnow
from tests.factories import (grant_permission, make_community,
                             make_community_member, make_instance, make_post,
                             make_post_reply, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def url(app, endpoint, **values):
    from flask import url_for

    with app.test_request_context():
        return url_for(endpoint, **values)


def public_instance():
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    return site


@pytest.fixture
def env(app, db_session):
    """A local community with one member, on a public instance."""
    public_instance()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    author = make_user(local, 'author', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return app.test_client(), community, author


def show(app, client, community, query=''):
    """GET the community page with render_template patched, and return the
    mock so a row can read the context the template would have had."""
    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get(url(app, 'activitypub.community_profile',
                                  actor=community.name) + query)
    return response, render


def context(render, name):
    return render.call_args.kwargs[name]


def moderator(user, community, owner=False):
    """The fixture already made `author` a member, and CommunityMember's
    primary key is (user_id, community_id), so a second row is a
    UniqueViolation. Promote the row that exists."""
    from app.models import CommunityMember

    member = CommunityMember.query.filter_by(user_id=user.id,
                                             community_id=community.id).first()
    if member is None:
        member = make_community_member(user, community)
    member.is_moderator = True
    member.is_owner = owner
    db.session.commit()
    return member


def a_post(community, author, number=1, **columns):
    post = make_post(community, author,
                     f'https://test.piefed.local/p/{number}',
                     title=f'post {number}')
    for column, value in columns.items():
        setattr(post, column, value)
    db.session.commit()
    return post


# --------------------------------------------------------------------------
# Who may see the page at all
# --------------------------------------------------------------------------


def test_a_banned_community_is_not_found(app, env):
    """`if community.banned: abort(404)` -- 404 rather than 403, because a
    banned community should not be distinguishable from one that never
    existed."""
    client, community, author = env
    community.banned = True
    db.session.commit()

    response, render = show(app, client, community)

    assert response.status_code == 404
    assert render.call_args is None


def test_a_private_community_refuses_a_non_member(app, env):
    """`Community.private` is invite-only access control, and this is the page
    a browser actually reaches."""
    client, community, author = env
    community.private = True
    db.session.commit()
    stranger = make_user(instance(), 'stranger', local=True)
    db.session.commit()
    login(client, stranger)

    response, render = show(app, client, community)

    assert response.status_code == 403
    assert render.call_args is None


def test_a_private_community_admits_a_member(app, env):
    """The other side of that condition."""
    client, community, author = env
    community.private = True
    db.session.commit()
    login(client, author)

    response, render = show(app, client, community)

    assert response.status_code == 200
    assert context(render, 'community').id == community.id


@pytest.mark.parametrize('content_warning, flags, redirected', [
    (True, {'nsfl': True}, True),
    (True, {'nsfw': True}, False),
    (False, {'nsfw': True}, True),
    (False, {'nsfl': True}, True),
    (False, {}, False),
])
def test_an_anonymous_visitor_and_adult_communities(app, env, content_warning,
                                                    flags, redirected):
    """Two configurations, and they differ. With `CONTENT_WARNING` on, the
    template puts an interstitial in front of NSFW content, so only NSFL
    communities are hidden from anonymous visitors; with it off, both are.
    A row for one configuration says nothing about the other."""
    client, community, author = env
    for column, value in flags.items():
        setattr(community, column, value)
    db.session.commit()

    app.config['CONTENT_WARNING'] = content_warning
    if content_warning:
        # `login_required_if_private_instance` sends every visitor without the
        # `warned` cookie to /content_warning before the route runs
        # (app/utils.py:1930), so the cookie is what lets a row reach the
        # community's own nsfw/nsfl handling at all.
        client.set_cookie('warned', '1', domain='test.piefed.local')
    try:
        response, render = show(app, client, community)
    finally:
        app.config['CONTENT_WARNING'] = False

    if redirected:
        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']
        assert render.call_args is None
    else:
        assert response.status_code == 200


def test_the_login_redirect_names_the_community_to_come_back_to(app, env):
    """`next_url = "/c/" + (community.ap_id if community.ap_id else
    community.name)` -- the point of the redirect is that logging in lands the
    visitor back where they were."""
    client, community, author = env
    community.nsfl = True
    db.session.commit()

    response, _render = show(app, client, community)

    assert 'next=/c/general' in response.headers['Location']


# --------------------------------------------------------------------------
# The viewer's relationship to the community
# --------------------------------------------------------------------------


def test_a_moderator_is_told_they_are_one(app, env):
    client, community, author = env
    moderator(author, community)
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'is_moderator') is True
    assert context(render, 'is_owner') is False


def test_an_owner_is_both(app, env):
    """`is_owner` is a separate flag read from the same rows, so an owner has
    to be both."""
    client, community, author = env
    moderator(author, community, owner=True)
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'is_moderator') is True
    assert context(render, 'is_owner') is True


def test_an_admin_is_told_they_are_one(app, env):
    client, community, author = env
    founder = db.session.get(User, 1)
    login(client, founder)

    _response, render = show(app, client, community)

    assert context(render, 'is_admin') is True


def test_an_anonymous_visitor_is_none_of_the_three(app, env):
    """The `else:` arm. All three flags drive moderation controls in the
    template."""
    client, community, author = env

    _response, render = show(app, client, community)

    assert context(render, 'is_moderator') is False
    assert context(render, 'is_owner') is False
    assert context(render, 'is_admin') is False


def test_a_banned_moderator_loses_the_moderation_controls(app, env):
    """The `and community.id not in communities_banned_from(...)` conjunct: a
    moderator banned from their own community is not offered the tools."""
    client, community, author = env
    moderator(author, community)
    db.session.add(CommunityBan(user_id=author.id, community_id=community.id,
                                banned_by=1))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'is_moderator') is False
    assert context(render, 'banned_from_community') is True


def test_a_banned_visitor_is_told_so(app, env):
    client, community, author = env
    db.session.add(CommunityBan(user_id=author.id, community_id=community.id,
                                banned_by=1))
    db.session.commit()
    login(client, author)

    with patch('app.community.routes.flash') as flashed:
        show(app, client, community)

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'banned from this community' in messages
    assert 'until' not in messages


def test_a_temporary_ban_names_the_date(app, env):
    """`if ban_details.ban_until:` -- a ban with an end date says when, and
    that is the whole difference between the two messages."""
    client, community, author = env
    until = utcnow() + timedelta(days=3)
    db.session.add(CommunityBan(user_id=author.id, community_id=community.id,
                                banned_by=1, ban_until=until))
    db.session.commit()
    login(client, author)

    with patch('app.community.routes.flash') as flashed:
        show(app, client, community)

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert str(until.date()) in messages


def test_a_banned_instance_bans_the_visitor_from_the_community(app, env):
    """A second, independent route to `banned_from_community`: the viewer has
    banned the instance the community lives on."""
    from app.models import InstanceBan

    client, community, author = env
    other = make_instance('other.example', software='piefed')
    community.instance_id = other.id
    db.session.commit()
    db.session.add(InstanceBan(user_id=author.id, instance_id=other.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'banned_from_community') is True


# --------------------------------------------------------------------------
# Moderators in the sidebar
# --------------------------------------------------------------------------


def test_the_moderators_are_listed(app, env):
    """A moderator row puts its user in the sidebar list."""
    client, community, author = env
    moderator(author, community)

    _response, render = show(app, client, community)

    assert author.id in [mod.id for mod in context(render, 'mods')]


def test_a_founder_without_a_moderator_row_is_not_listed(app, env):
    """D174, fixed (owner ruling): `community_moderators` returns only real
    community_member moderator rows; `community.user_id` without one is no
    longer synthesised into the list."""
    client, community, author = env
    community.user_id = author.id
    db.session.commit()

    _response, render = show(app, client, community)

    assert author.id not in [mod.id for mod in context(render, 'mods')]


def test_private_mods_are_not_listed(app, env):
    """`community.private_mods` hides the list; it does not stop the page."""
    client, community, author = env
    moderator(author, community)
    community.private_mods = True
    db.session.commit()

    _response, render = show(app, client, community)

    assert context(render, 'mods') == []


def test_a_deleted_moderator_is_not_listed(app, env):
    client, community, author = env
    moderator(author, community)
    author.deleted = True
    db.session.commit()

    _response, render = show(app, client, community)

    assert author.id not in [mod.id for mod in context(render, 'mods')]


def an_admin_who_is_not_a_mod(community):
    """The viewer cannot be one of the moderators under test: Flask-Login's
    request handling stamps `last_seen` on the CURRENT user, so an admin who
    is also a mod makes themselves active again just by loading the page."""
    admin = make_user(instance(), 'sysadmin', local=True)
    db.session.commit()
    grant_permission(admin, 'change instance settings')
    admin.roles[0].name = 'Admin'
    db.session.commit()
    return admin


def test_a_community_whose_mods_are_all_inactive_is_flagged(app, env):
    """`un_moderated` drives an admin-only warning, and is computed only for
    an admin or staff member -- the count of inactive mods against the count
    of mods."""
    client, community, author = env
    moderator(author, community)
    founder = db.session.get(User, 1)
    admin = an_admin_who_is_not_a_mod(community)
    author.last_seen = utcnow() - timedelta(days=90)
    founder.last_seen = utcnow() - timedelta(days=90)
    db.session.commit()
    login(client, admin)

    _response, render = show(app, client, community)

    assert context(render, 'un_moderated') is True


def test_a_community_with_an_active_mod_is_not_flagged(app, env):
    client, community, author = env
    moderator(author, community)
    founder = db.session.get(User, 1)
    admin = an_admin_who_is_not_a_mod(community)
    author.last_seen = utcnow()
    founder.last_seen = utcnow()
    db.session.commit()
    login(client, admin)

    _response, render = show(app, client, community)

    assert context(render, 'un_moderated') is False


def test_an_ordinary_visitor_never_sees_the_flag(app, env):
    """The flag is computed behind `current_user.is_admin() or
    current_user.is_staff()`, so for anyone else it stays at its default."""
    client, community, author = env
    moderator(author, community)
    author.last_seen = utcnow() - timedelta(days=90)
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'un_moderated') is False


def test_user_flair_is_collected_for_the_sidebar(app, env):
    client, community, author = env
    db.session.add(UserFlair(user_id=author.id, community_id=community.id,
                             flair='Regular'))
    db.session.commit()

    _response, render = show(app, client, community)

    assert context(render, 'user_flair') == {author.id: 'Regular'}


# --------------------------------------------------------------------------
# Which posts are listed
# --------------------------------------------------------------------------


def titles(render, key='posts'):
    listing = render.call_args.kwargs[key]
    return [item.title for item in listing.items]


def test_the_communitys_posts_are_listed(app, env):
    client, community, author = env
    a_post(community, author, 1)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_another_communitys_posts_are_not(app, env):
    """`Post.query.filter(Post.community_id == community.id)` -- the one filter
    the whole listing rests on."""
    client, community, author = env
    a_post(community, author, 1)
    elsewhere = make_community('elsewhere')
    db.session.commit()
    a_post(elsewhere, author, 2)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_an_events_listing_shows_only_events(app, env):
    """`content_type=events` narrows the same query to `Post.type ==
    POST_TYPE_EVENT`."""
    from app.constants import POST_TYPE_EVENT

    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, type=POST_TYPE_EVENT)

    _response, render = show(app, client, community, '?content_type=events')

    assert titles(render) == ['post 2']


@pytest.mark.parametrize('column', ['deleted'])
def test_a_removed_post_is_not_listed(app, env, column):
    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, **{column: True})

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_a_post_under_review_is_not_listed(app, env):
    """`Post.status > POST_STATUS_REVIEWING` -- a post held for review has
    never been public."""
    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, status=POST_STATUS_REVIEWING)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


@pytest.mark.parametrize('content_warning, hidden', [
    (True, ['post nsfl']),
    (False, ['post nsfw', 'post nsfl']),
])
def test_an_anonymous_visitor_and_adult_posts(app, env, content_warning,
                                              hidden):
    """The two configurations filter differently, exactly as the community
    gate above does: with `CONTENT_WARNING` on, NSFW posts stay and the
    template warns; with it off they are filtered out."""
    client, community, author = env
    plain = a_post(community, author, 1)
    nsfw = a_post(community, author, 2, nsfw=True)
    nsfl = a_post(community, author, 3, nsfl=True)
    for post, name in ((plain, 'post plain'), (nsfw, 'post nsfw'),
                       (nsfl, 'post nsfl')):
        post.title = name
    db.session.commit()

    app.config['CONTENT_WARNING'] = content_warning
    if content_warning:
        client.set_cookie('warned', '1', domain='test.piefed.local')
    try:
        _response, render = show(app, client, community)
    finally:
        app.config['CONTENT_WARNING'] = False

    shown = titles(render)
    for title in hidden:
        assert title not in shown
    assert 'post plain' in shown


def test_an_anonymous_visitor_does_not_see_bots(app, env):
    """`Post.from_bot == False` for anonymous visitors only -- a logged-in
    user's own `ignore_bots` preference decides for them."""
    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, from_bot=True)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


@pytest.mark.parametrize('preference, column', [
    ('ignore_bots', 'from_bot'),
    ('hide_nsfl', 'nsfl'),
    ('hide_nsfw', 'nsfw'),
    ('hide_gen_ai', 'ai_generated'),
])
def test_a_logged_in_visitors_content_preferences(app, env, preference,
                                                  column):
    """Four independent preferences, four independent filters.

    Each row asserts BOTH directions, because three of the four default to a
    value that already hides the post -- so a row that only set the preference
    to 1 would pass against a filter that ignored the preference entirely.
    Every other preference is cleared so that a filter reading the wrong
    column is not covered by another filter doing the same job.
    """
    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, **{column: True})
    for other in ('ignore_bots', 'hide_nsfl', 'hide_nsfw', 'hide_gen_ai'):
        setattr(author, other, 0)
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)
    assert sorted(titles(render)) == ['post 1', 'post 2']

    setattr(author, preference, 1)
    db.session.commit()

    _response, render = show(app, client, community)
    assert titles(render) == ['post 1']


def test_a_logged_in_visitor_without_the_preference_sees_both(app, env):
    """The control for the four rows above: with the preferences off, none of
    those posts is filtered.

    The defaults are NOT off -- `hide_nsfw` and `hide_nsfl` default to 1 and
    `hide_gen_ai` to 2 (`app/models.py:993-995`), and only `ignore_bots`
    starts at 0. Each filter tests `== 1`, so `hide_gen_ai = 2` means "label
    it", not "hide it". A control row has to clear them explicitly."""
    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, nsfw=True, nsfl=True, from_bot=True,
           ai_generated=True)
    author.hide_nsfw = 0
    author.hide_nsfl = 0
    author.hide_gen_ai = 0
    author.ignore_bots = 0
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert sorted(titles(render)) == ['post 1', 'post 2']


def test_read_posts_can_be_hidden(app, env):
    """`hide_read_posts` joins `read_posts` and keeps only the rows with no
    match."""
    from app.models import read_posts

    client, community, author = env
    a_post(community, author, 1)
    read = a_post(community, author, 2)
    db.session.execute(read_posts.insert().values(user_id=author.id,
                                                  read_post_id=read.id,
                                                  interacted_at=utcnow()))
    author.hide_read_posts = True
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_a_tag_filter_overrides_hide_read_posts(app, env):
    """`if current_user.hide_read_posts and not tag:` -- asking for a tag is
    asking for everything with that tag, read or not."""
    from app.models import Tag, post_tag, read_posts

    client, community, author = env
    read = a_post(community, author, 2)
    tag = Tag(name='news', display_as='news')
    db.session.add(tag)
    db.session.commit()
    db.session.execute(post_tag.insert().values(post_id=read.id,
                                                tag_id=tag.id))
    db.session.execute(read_posts.insert().values(user_id=author.id,
                                                  read_post_id=read.id,
                                                  interacted_at=utcnow()))
    author.hide_read_posts = True
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community, '?tag=news')

    assert titles(render) == ['post 2']


def test_a_hidden_post_is_not_listed(app, env):
    """`hidden_posts` is an unconditional filter for a logged-in visitor --
    no preference guards it."""
    from app.models import hidden_posts

    client, community, author = env
    a_post(community, author, 1)
    hidden = a_post(community, author, 2)
    db.session.execute(hidden_posts.insert().values(user_id=author.id,
                                                    hidden_post_id=hidden.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_a_blocked_domain_hides_its_posts(app, env):
    from app.models import Domain, DomainBlock

    client, community, author = env
    a_post(community, author, 1)
    blocked = a_post(community, author, 2)
    domain = Domain(name='blocked.example')
    db.session.add(domain)
    db.session.commit()
    blocked.domain_id = domain.id
    db.session.add(DomainBlock(user_id=author.id, domain_id=domain.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_a_blocked_instance_hides_its_posts(app, env):
    from app.models import InstanceBlock

    client, community, author = env
    a_post(community, author, 1)
    blocked = a_post(community, author, 2)
    other = make_instance('other.example', software='piefed')
    blocked.instance_id = other.id
    db.session.add(InstanceBlock(user_id=author.id, instance_id=other.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_a_blocked_user_hides_their_posts(app, env):
    from app.models import UserBlock

    client, community, author = env
    a_post(community, author, 1)
    nuisance = make_user(instance(), 'nuisance', local=True)
    db.session.commit()
    a_post(community, nuisance, 2)
    db.session.add(UserBlock(blocker_id=author.id, blocked_id=nuisance.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


# --------------------------------------------------------------------------
# Flair and tags
# --------------------------------------------------------------------------


def flair_on(post, community, name='Question'):
    from app.models import post_flair

    flair = CommunityFlair(community_id=community.id, flair=name,
                           text_color='#000000', background_color='#ffffff')
    db.session.add(flair)
    db.session.commit()
    db.session.execute(post_flair.insert().values(post_id=post.id,
                                                  flair_id=flair.id))
    db.session.commit()
    return flair


def test_a_flair_filter_narrows_the_listing(app, env):
    client, community, author = env
    a_post(community, author, 1)
    flaired = a_post(community, author, 2)
    flair_on(flaired, community)

    _response, render = show(app, client, community, '?flair=Question')

    assert titles(render) == ['post 2']


def test_an_unknown_flair_filters_nothing(app, env):
    """`if flair_id:` -- `find_flair_id` returns None for a name this
    community does not have, and the listing is then unfiltered rather than
    empty."""
    client, community, author = env
    a_post(community, author, 1)

    _response, render = show(app, client, community, '?flair=Nonexistent')

    assert titles(render) == ['post 1']


def test_blocked_flair_hides_its_posts(app, env):
    client, community, author = env
    a_post(community, author, 1)
    flaired = a_post(community, author, 2)
    flair = flair_on(flaired, community)
    db.session.add(CommunityFlairBlock(user_id=author.id,
                                       community_id=community.id,
                                       community_flair_id=flair.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_asking_for_blocked_flair_shows_it(app, env):
    """`if bf.community_flair_id != flair_id` -- the block is dropped for the
    flair the visitor has explicitly asked to see."""
    client, community, author = env
    flaired = a_post(community, author, 2)
    flair = flair_on(flaired, community)
    db.session.add(CommunityFlairBlock(user_id=author.id,
                                       community_id=community.id,
                                       community_flair_id=flair.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community, '?flair=Question')

    assert titles(render) == ['post 2']


def test_a_tag_filter_narrows_the_listing(app, env):
    from app.models import Tag, post_tag

    client, community, author = env
    a_post(community, author, 1)
    tagged = a_post(community, author, 2)
    tag = Tag(name='news', display_as='news')
    db.session.add(tag)
    db.session.commit()
    db.session.execute(post_tag.insert().values(post_id=tagged.id,
                                                tag_id=tag.id))
    db.session.commit()

    _response, render = show(app, client, community, '?tag=news')

    assert titles(render) == ['post 2']


def test_an_unknown_tag_filters_nothing(app, env):
    """`if tag_record:` -- the same shape as the flair lookup, and the same
    consequence when it misses."""
    client, community, author = env
    a_post(community, author, 1)

    _response, render = show(app, client, community, '?tag=nonexistent')

    assert titles(render) == ['post 1']


# --------------------------------------------------------------------------
# Sticky posts and sorting
# --------------------------------------------------------------------------


def test_sticky_posts_are_listed_separately(app, env):
    """`sticky_posts` and `posts` are two queries over the same filters, split
    on `Post.sticky`, so a sticky post must appear in one and not the other."""
    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, sticky=True)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']
    assert [post.title for post in context(render, 'sticky_posts')] == ['post 2']


@pytest.mark.parametrize('sort, expected', [
    ('new', ['newest', 'oldest']),
    ('old', ['oldest', 'newest']),
])
def test_the_date_sorts(app, env, sort, expected):
    client, community, author = env
    old = a_post(community, author, 1, posted_at=utcnow() - timedelta(days=10))
    new = a_post(community, author, 2, posted_at=utcnow())
    old.title, new.title = 'oldest', 'newest'
    db.session.commit()

    _response, render = show(app, client, community, f'?sort={sort}')

    assert titles(render) == expected


@pytest.mark.parametrize('sort, within', [
    ('top_12h', timedelta(hours=6)),
    ('top', timedelta(hours=18)),
    ('top_1w', timedelta(days=3)),
    ('top_1m', timedelta(days=20)),
    ('top_1y', timedelta(days=200)),
])
def test_the_windowed_top_sorts_drop_older_posts(app, env, sort, within):
    """Each `top_*` sort is a different window, and every one of them filters
    as well as orders -- a post outside the window is not listed at all."""
    client, community, author = env
    inside = a_post(community, author, 1, posted_at=utcnow() - within)
    outside = a_post(community, author, 2,
                     posted_at=utcnow() - timedelta(days=400))
    inside.title, outside.title = 'inside', 'outside'
    db.session.commit()

    _response, render = show(app, client, community, f'?sort={sort}')

    assert titles(render) == ['inside']


def test_top_all_has_no_window(app, env):
    """`top_all` is the one `top_*` sort that does not filter by date."""
    client, community, author = env
    a_post(community, author, 1, posted_at=utcnow() - timedelta(days=4000),
           up_votes=5)
    a_post(community, author, 2, up_votes=1)

    _response, render = show(app, client, community, '?sort=top_all')

    assert titles(render) == ['post 1', 'post 2']


def test_the_active_sort_hides_posts_with_no_replies(app, env):
    """`sort=active` filters `Post.reply_count > 0` -- the only sort that
    changes which posts exist rather than only their order."""
    client, community, author = env
    a_post(community, author, 1, reply_count=2)
    a_post(community, author, 2, reply_count=0)

    _response, render = show(app, client, community, '?sort=active')

    assert titles(render) == ['post 1']


def test_the_scaled_sort_falls_back_to_the_default(app, env):
    """`if sort == 'scaled': sort = ''` -- the community page has no scaled
    ranking, and an unrecognised sort would order by nothing at all."""
    client, community, author = env
    a_post(community, author, 1)

    _response, render = show(app, client, community, '?sort=scaled')

    assert context(render, 'sort') == ''


def test_a_logged_in_visitors_default_sort_is_used(app, env):
    """The default for the `sort` parameter is the viewer's own setting, so a
    plain visit to the page is already sorted the way they asked."""
    client, community, author = env
    author.default_sort = 'new'
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'sort') == 'new'


# --------------------------------------------------------------------------
# The comments view, and D1005
# --------------------------------------------------------------------------


def bodies(render):
    return [reply.body for reply in context(render, 'comments').items]


def test_the_communitys_comments_are_listed(app, env):
    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='a reply')

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == ['a reply']
    assert context(render, 'posts') is None


def test_a_deleted_comment_is_not_listed(app, env):
    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='a reply')
    gone = make_post_reply(post, author, body='a deleted reply')
    gone.deleted = True
    db.session.commit()

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == ['a reply']


def test_replies_to_a_removed_post_are_not_listed(app, env):
    """D1005. `Community.replies` is every PostReply in the community, with no
    join to Post, so the comments view listed the discussion under a post a
    moderator had removed -- the post itself is refused by the posts view two
    lines away. Measured before the fix:
    `PROBE s1 replies shown: ['reply to a removed post']`."""
    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='reply to a removed post')
    post.deleted = True
    db.session.commit()

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == []


def test_replies_to_a_post_under_review_are_not_listed(app, env):
    """D1005's second half, and the one with the larger blast radius: a post
    awaiting review has never been public, and its replies were."""
    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='reply to a pending post')
    post.status = POST_STATUS_REVIEWING
    db.session.commit()

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == []


def test_replies_to_a_published_post_are_still_listed(app, env):
    """The control for the two rows above: the join D1005 adds must not hide
    the ordinary case."""
    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='an ordinary reply')

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == ['an ordinary reply']


def test_an_anonymous_visitor_does_not_see_adult_or_bot_comments(app, env):
    """PostReply has no `nsfl` column (`app/models.py:2892`), so the comments
    branch filters `nsfw` and `from_bot` only -- fewer filters than the posts
    branch, and not an omission."""
    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='plain')
    adult = make_post_reply(post, author, body='adult')
    adult.nsfw = True
    bot = make_post_reply(post, author, body='bot')
    bot.from_bot = True
    db.session.commit()

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == ['plain']


@pytest.mark.parametrize('preference, column', [
    ('ignore_bots', 'from_bot'),
    ('hide_nsfw', 'nsfw'),
])
def test_a_logged_in_visitors_comment_preferences(app, env, preference,
                                                  column):
    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='plain')
    flagged = make_post_reply(post, author, body='flagged')
    setattr(flagged, column, True)
    for other in ('ignore_bots', 'hide_nsfw'):
        setattr(author, other, 0)
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community, '?content_type=comments')
    assert sorted(bodies(render)) == ['flagged', 'plain']

    setattr(author, preference, 1)
    db.session.commit()

    _response, render = show(app, client, community, '?content_type=comments')
    assert bodies(render) == ['plain']


def test_a_blocked_instances_comments_are_not_listed(app, env):
    from app.models import InstanceBlock

    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='plain')
    other = make_instance('other.example', software='piefed')
    remote_author = make_user(other, 'remote')
    db.session.commit()
    make_post_reply(post, remote_author, body='remote')
    db.session.add(InstanceBlock(user_id=author.id, instance_id=other.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == ['plain']


def test_a_blocked_users_comments_are_not_listed(app, env):
    from app.models import UserBlock

    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='plain')
    nuisance = make_user(instance(), 'nuisance', local=True)
    db.session.commit()
    make_post_reply(post, nuisance, body='nuisance')
    db.session.add(UserBlock(blocker_id=author.id, blocked_id=nuisance.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == ['plain']


@pytest.mark.parametrize('sort, expected', [
    ('new', ['newest', 'oldest']),
    ('old', ['oldest', 'newest']),
    ('', ['newest', 'oldest']),
])
def test_the_comment_date_sorts(app, env, sort, expected):
    client, community, author = env
    post = a_post(community, author, 1)
    old = make_post_reply(post, author, body='oldest')
    new = make_post_reply(post, author, body='newest')
    old.posted_at = utcnow() - timedelta(days=10)
    new.posted_at = utcnow()
    db.session.commit()

    _response, render = show(app, client, community,
                             f'?content_type=comments&sort={sort}')

    assert bodies(render) == expected


@pytest.mark.parametrize('sort, within', [
    ('top_12h', timedelta(hours=6)),
    ('top', timedelta(hours=18)),
    ('top_1w', timedelta(days=3)),
    ('top_1m', timedelta(days=20)),
    ('top_1y', timedelta(days=200)),
])
def test_the_windowed_comment_sorts_drop_older_replies(app, env, sort, within):
    client, community, author = env
    post = a_post(community, author, 1)
    inside = make_post_reply(post, author, body='inside')
    outside = make_post_reply(post, author, body='outside')
    inside.posted_at = utcnow() - within
    outside.posted_at = utcnow() - timedelta(days=400)
    db.session.commit()

    _response, render = show(app, client, community,
                             f'?content_type=comments&sort={sort}')

    assert bodies(render) == ['inside']


def test_top_all_comments_have_no_window(app, env):
    client, community, author = env
    post = a_post(community, author, 1)
    old = make_post_reply(post, author, body='old but popular')
    old.posted_at = utcnow() - timedelta(days=4000)
    old.up_votes = 9
    db.session.commit()

    _response, render = show(app, client, community,
                             '?content_type=comments&sort=top_all')

    assert bodies(render) == ['old but popular']


# --------------------------------------------------------------------------
# Layout, paging and caching
# --------------------------------------------------------------------------


def test_the_community_chooses_the_default_layout(app, env):
    client, community, author = env
    community.default_layout = 'masonry'
    db.session.commit()

    _response, render = show(app, client, community)

    assert context(render, 'post_layout') == 'masonry'


def test_a_community_with_no_default_layout_gets_a_list(app, env):
    client, community, author = env
    community.default_layout = ''
    db.session.commit()

    _response, render = show(app, client, community)

    assert context(render, 'post_layout') == 'list'


def test_the_query_parameter_overrides_the_communitys_layout(app, env):
    client, community, author = env
    community.default_layout = 'masonry'
    db.session.commit()

    _response, render = show(app, client, community, '?layout=list')

    assert context(render, 'post_layout') == 'list'


def test_low_bandwidth_drops_the_layout_entirely(app, env):
    """The `low_bandwidth` cookie sets `post_layout = None`, which the template
    reads as "no cards, no images"."""
    client, community, author = env
    client.set_cookie('low_bandwidth', '1', domain='test.piefed.local')

    _response, render = show(app, client, community)

    assert context(render, 'post_layout') is None
    assert context(render, 'low_bandwidth') is True


@pytest.mark.parametrize('layout, expected', [
    ('masonry', 200), ('masonry_wide', 300),
])
def test_the_masonry_layouts_page_in_larger_blocks(app, env, layout,
                                                   expected):
    """`per_page` is 200 for masonry and 300 for masonry_wide, because the
    layout only looks right when it is full."""
    client, community, author = env
    a_post(community, author, 1)

    _response, render = show(app, client, community, f'?layout={layout}')

    assert context(render, 'posts').per_page == expected


def test_a_visitors_page_length_is_respected_when_smaller(app, env):
    """`if current_user.page_length and current_user.page_length < per_page` --
    the preference may shorten the page, never lengthen it."""
    client, community, author = env
    a_post(community, author, 1)
    author.page_length = 3
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'posts').per_page == 3


def test_a_larger_page_length_is_ignored(app, env):
    client, community, author = env
    a_post(community, author, 1)
    author.page_length = 9999
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'posts').per_page == app.config['PAGE_LENGTH']


def test_a_second_page_offers_a_link_back(app, env):
    """`prev_url` is suppressed on page 1 (`and page != 1`), so a row for it
    has to ask for page 2."""
    client, community, author = env
    author.page_length = 1
    db.session.commit()
    a_post(community, author, 1)
    a_post(community, author, 2)
    login(client, author)

    _response, render = show(app, client, community, '?page=2')

    assert context(render, 'prev_url') is not None
    assert context(render, 'next_url') is None


def test_the_first_page_offers_only_a_link_forward(app, env):
    client, community, author = env
    author.page_length = 1
    db.session.commit()
    a_post(community, author, 1)
    a_post(community, author, 2)
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'prev_url') is None
    assert context(render, 'next_url') is not None


def test_the_comments_view_pages_separately(app, env):
    """The next/prev pair is built from `comments` rather than `posts` in the
    comments branch, and a row over posts cannot see that."""
    client, community, author = env
    post = a_post(community, author, 1)
    for number in range(120):
        make_post_reply(post, author, body=f'reply {number}')

    _response, render = show(app, client, community, '?content_type=comments')

    assert context(render, 'next_url') is not None
    assert 'content_type=comments' in context(render, 'next_url')


def test_an_anonymous_visitor_gets_a_public_cache(app, env):
    """Anonymous responses carry an ETag and a public cache directive; a
    logged-in one must not, or a shared cache serves one visitor's page to
    another."""
    client, community, author = env

    response, render = show(app, client, community)

    assert response.headers['Cache-Control'] == 'public, max-age=30'
    assert response.headers['ETag'].strip('"') == context(render, 'etag')
    assert 'Cookie' not in response.headers['Vary']


def test_a_logged_in_visitor_gets_a_private_cache(app, env):
    client, community, author = env
    login(client, author)

    response, _render = show(app, client, community)

    assert response.headers['Cache-Control'] == 'private, max-age=15, must-revalidate'
    assert 'Cookie' in response.headers['Vary']
    assert 'ETag' not in response.headers


def test_an_unchanged_page_is_a_304(app, env):
    """`request_etag_matches` short-circuits the whole function for an
    anonymous visitor whose ETag still matches."""
    client, community, author = env
    first, render = show(app, client, community)
    etag = first.headers['ETag']

    with patch('app.community.routes.render_template',
               return_value='rendered') as second_render:
        second = client.get(url(app, 'activitypub.community_profile',
                                actor=community.name),
                            headers={'If-None-Match': etag})

    assert second.status_code == 304
    assert second_render.call_args is None


def test_a_logged_in_visitor_never_gets_a_304(app, env):
    """The 304 is gated on `current_user.is_anonymous`, because the page
    differs per visitor -- so the same ETag that short-circuits an anonymous
    request must still render for a logged-in one."""
    client, community, author = env
    login(client, author)
    _first, render = show(app, client, community)
    etag = context(render, 'etag')

    with patch('app.community.routes.render_template',
               return_value='rendered') as second_render:
        response = client.get(url(app, 'activitypub.community_profile',
                                  actor=community.name),
                              headers={'If-None-Match': f'"{etag}"'})

    assert response.status_code == 200
    assert second_render.call_args is not None


# --------------------------------------------------------------------------
# Breadcrumbs, related communities, and D1006
# --------------------------------------------------------------------------


def crumbs(render):
    return [crumb.text for crumb in context(render, 'breadcrumbs')]


def test_a_community_with_no_topic_or_feed_leads_back_to_communities(app, env):
    client, community, author = env

    _response, render = show(app, client, community)

    assert crumbs(render)[-1] == 'Communities'
    assert context(render, 'related_communities') == []


def test_a_communitys_topic_is_in_the_breadcrumbs(app, env):
    client, community, author = env
    topic = Topic(name='Science', machine_name='science')
    db.session.add(topic)
    db.session.commit()
    community.topic_id = topic.id
    db.session.commit()

    _response, render = show(app, client, community)

    assert crumbs(render) == ['Home', 'Topics', 'Science']


def test_the_whole_topic_path_is_walked(app, env):
    """The walk climbs `parent_id` until it runs out, and the list is reversed
    so the root comes first."""
    client, community, author = env
    root = Topic(name='Science', machine_name='science')
    db.session.add(root)
    db.session.commit()
    child = Topic(name='Physics', machine_name='physics', parent_id=root.id)
    db.session.add(child)
    db.session.commit()
    community.topic_id = child.id
    db.session.commit()

    _response, render = show(app, client, community)

    assert crumbs(render) == ['Home', 'Topics', 'Science', 'Physics']


def test_a_topic_whose_parent_has_been_deleted_is_not_a_500(app, env):
    """D1006. `db.session.get` returns None for a `parent_id` pointing at a
    topic that no longer exists, and the next iteration read `.parent_id` off
    it. Measured: `AttributeError: 'NoneType' object has no attribute
    'parent_id'`."""
    client, community, author = env
    orphan = Topic(name='Orphan', machine_name='orphan', parent_id=9999)
    db.session.add(orphan)
    db.session.commit()
    community.topic_id = orphan.id
    db.session.commit()

    response, render = show(app, client, community)

    assert response.status_code == 200
    assert crumbs(render) == ['Home', 'Topics', 'Orphan']


def test_a_topic_cycle_does_not_walk_forever(app, env):
    """The other way that loop does not end. Two topics each naming the other
    as parent is a database a migration or a hand-edit can produce, and the
    page would hang rather than fail."""
    client, community, author = env
    first = Topic(name='First', machine_name='first')
    second = Topic(name='Second', machine_name='second')
    db.session.add_all([first, second])
    db.session.commit()
    first.parent_id = second.id
    second.parent_id = first.id
    community.topic_id = first.id
    db.session.commit()

    response, render = show(app, client, community)

    assert response.status_code == 200
    assert crumbs(render) == ['Home', 'Topics', 'Second', 'First']


def test_related_communities_share_the_topic(app, env):
    client, community, author = env
    topic = Topic(name='Science', machine_name='science')
    db.session.add(topic)
    db.session.commit()
    sibling = make_community('sibling')
    banned = make_community('banned_sibling')
    community.topic_id = topic.id
    sibling.topic_id = topic.id
    banned.topic_id = topic.id
    banned.banned = True
    db.session.commit()

    _response, render = show(app, client, community)

    names = [related.name for related in context(render, 'related_communities')]
    assert names == ['sibling']


def test_a_community_in_a_public_feed_leads_back_to_feeds(app, env):
    client, community, author = env
    feed = Feed(user_id=1, title='News', name='news', public=True,
                ap_profile_id='https://test.piefed.local/f/news')
    db.session.add(feed)
    db.session.commit()
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    db.session.commit()

    _response, render = show(app, client, community)

    assert crumbs(render) == ['Home', 'Feeds', 'News']


def test_a_feed_cycle_does_not_walk_forever(app, env):
    """D1006's second site: the same walk, over `parent_feed_id`.

    Only the cycle half is reachable here. `feed.parent_feed_id` carries a
    foreign key (`feed_parent_feed_id_fkey`), so a dangling parent cannot be
    stored -- the `if feed is None: break` on that walk is defence against the
    constraint being relaxed, not a live case. `Topic.parent_id` has no such
    constraint, which is why the topic walk above CAN be given a missing
    parent. A feed naming itself as its own parent satisfies the key and
    walked forever.
    """
    client, community, author = env
    feed = Feed(user_id=1, title='News', name='news', public=True,
                ap_profile_id='https://test.piefed.local/f/news')
    db.session.add(feed)
    db.session.commit()
    feed.parent_feed_id = feed.id
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    db.session.commit()

    response, render = show(app, client, community)

    assert response.status_code == 200
    assert crumbs(render) == ['Home', 'Feeds', 'News']


def test_the_visitors_own_feed_is_named(app, env):
    """`user_has_feeds` and `current_feed_id` drive the "add to feed" control,
    and only the feed that already contains this community counts."""
    client, community, author = env
    feed = Feed(user_id=author.id, title='Mine', name='mine',
                ap_profile_id='https://test.piefed.local/f/mine')
    db.session.add(feed)
    db.session.commit()
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'user_has_feeds') is True
    assert context(render, 'current_feed_id') == feed.id
    assert context(render, 'current_feed_title') == 'Mine'


def test_a_feed_without_this_community_is_not_named(app, env):
    """`if current_feed is not None:` -- the visitor has feeds, but none of
    them holds this community."""
    client, community, author = env
    feed = Feed(user_id=author.id, title='Mine', name='mine',
                ap_profile_id='https://test.piefed.local/f/mine')
    db.session.add(feed)
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'user_has_feeds') is True
    assert context(render, 'current_feed_id') == 0
    assert context(render, 'current_feed_title') == 'None'


# --------------------------------------------------------------------------
# Events, votes, and the dead-instance warning
# --------------------------------------------------------------------------


def test_upcoming_events_are_listed(app, env):
    """The raw SQL reads the `event` table, orders by start and takes five, so
    a past event must not appear."""
    from app.constants import POST_TYPE_EVENT
    from app.models import Event

    client, community, author = env
    soon = a_post(community, author, 1, type=POST_TYPE_EVENT)
    past = a_post(community, author, 2, type=POST_TYPE_EVENT)
    db.session.add(Event(post_id=soon.id, start=utcnow() + timedelta(days=1),
                         end=utcnow() + timedelta(days=2)))
    db.session.add(Event(post_id=past.id, start=utcnow() - timedelta(days=2),
                         end=utcnow() - timedelta(days=1)))
    db.session.commit()

    _response, render = show(app, client, community)

    assert [row.title for row in context(render, 'upcoming_events')] == ['post 1']
    assert context(render, 'has_events') == 2


def test_a_community_with_no_events_says_so(app, env):
    client, community, author = env
    a_post(community, author, 1)

    _response, render = show(app, client, community)

    assert context(render, 'upcoming_events') == []
    assert context(render, 'has_events') == 0


def test_a_logged_in_visitors_votes_are_loaded(app, env):
    """`recently_upvoted` and `recently_downvoted` are empty lists for an
    anonymous visitor rather than a query."""
    client, community, author = env
    post = a_post(community, author, 1)
    from app.models import PostVote

    db.session.add(PostVote(user_id=author.id, post_id=post.id, effect=1,
                            author_id=author.id))
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert post.id in context(render, 'recently_upvoted')


def test_an_anonymous_visitor_has_no_votes(app, env):
    client, community, author = env

    _response, render = show(app, client, community)

    assert context(render, 'recently_upvoted') == []
    assert context(render, 'recently_downvoted') == []


def test_a_local_community_is_never_dead(app, env):
    client, community, author = env

    _response, render = show(app, client, community)

    assert context(render, 'is_dead') is False


def remote_community(name='general', host='other.example', gone=False):
    other = make_instance(host, software='piefed')
    other.gone_forever = gone
    community = make_community(name, host=host)
    community.ap_id = f'{name}@{host}'
    community.instance_id = other.id
    db.session.commit()
    return community


def test_a_community_on_a_dead_instance_warns(app, env):
    client, _local_community, author = env
    community = remote_community('remote', gone=True)

    with patch('app.community.routes.flash') as flashed:
        with patch('app.community.routes.render_template',
                   return_value='rendered') as render:
            response = client.get('/c/remote@other.example')

    assert response.status_code == 200
    assert context(render, 'is_dead') is True
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'no longer online' in messages


def test_a_community_on_a_live_remote_instance_does_not_warn(app, env):
    client, _local_community, author = env
    remote_community('remote', gone=False)

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/c/remote@other.example')

    assert response.status_code == 200
    assert context(render, 'is_dead') is False


def test_a_remote_community_with_no_instance_row_is_not_a_500(app, env):
    """D1007. `Community.instance_id` is nullable (`app/models.py:575`), so
    `community.instance.gone_forever` was an `AttributeError` -- measured. An
    unknown instance is not a dead one."""
    client, _local_community, author = env
    community = remote_community('remote')
    community.instance_id = None
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get('/c/remote@other.example')

    assert response.status_code == 200
    assert context(render, 'is_dead') is False


def test_a_visitor_with_no_default_sort_gets_the_default(app, env):
    """`if sort is None: sort = ''` -- `User.default_sort` is nullable, and
    the parameter's default is that column, so a visitor who has never chosen
    one arrives here with None and no `order_by` would be applied at all."""
    client, community, author = env
    a_post(community, author, 1)
    author.default_sort = None
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community)

    assert context(render, 'sort') == ''
    assert titles(render) == ['post 1']


def test_the_whole_feed_path_is_walked(app, env):
    """The feed walk climbs `parent_feed_id` the way the topic walk climbs
    `parent_id`, and the crumbs come out root-first."""
    client, community, author = env
    parent = Feed(user_id=1, title='World', name='world', public=True,
                  ap_profile_id='https://test.piefed.local/f/world')
    db.session.add(parent)
    db.session.commit()
    child = Feed(user_id=1, title='News', name='news', public=True,
                 parent_feed_id=parent.id,
                 ap_profile_id='https://test.piefed.local/f/news')
    db.session.add(child)
    db.session.commit()
    db.session.add(FeedItem(feed_id=child.id, community_id=community.id))
    db.session.commit()

    _response, render = show(app, client, community)

    assert crumbs(render) == ['Home', 'Feeds', 'World', 'News']


def test_a_logged_in_visitor_does_not_see_removed_posts(app, env):
    """The posts branch filters `Post.deleted` TWICE -- once for anonymous
    visitors and once for logged-in ones -- and a row for the anonymous filter
    says nothing about the other. Deleting the logged-in one changed nothing
    any assertion could see until this row existed."""
    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, deleted=True)
    login(client, author)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_a_logged_in_visitor_does_not_see_posts_under_review(app, env):
    """The other half of that same duplicated filter."""
    client, community, author = env
    a_post(community, author, 1)
    a_post(community, author, 2, status=POST_STATUS_REVIEWING)
    login(client, author)

    _response, render = show(app, client, community)

    assert titles(render) == ['post 1']


def test_a_logged_in_visitor_does_not_see_deleted_comments(app, env):
    """`PostReply.deleted` is filtered twice as well, in the two arms of the
    comments branch."""
    client, community, author = env
    post = a_post(community, author, 1)
    make_post_reply(post, author, body='a reply')
    gone = make_post_reply(post, author, body='a deleted reply')
    gone.deleted = True
    db.session.commit()
    login(client, author)

    _response, render = show(app, client, community, '?content_type=comments')

    assert bodies(render) == ['a reply']
