"""app/tag/routes.py -- the two tag lists, the ban pair, and the post list.

MEASUREMENT BASIS. The module stood at 47.688% on the full-suite --cov=app run
at 1bf93f295, carrying 119 missing statements. This round takes 77 of them --
`tags`, `tags_blocked_list`, `tag_ban`, `tag_unban` and `tag_posts`; sub-project
68 takes `tag_cloud` and closes the package.

Five defects are pinned here and repaired together:

  P1  tag_ban and tag_unban have no `else`, so a tag that is not found falls off
      the end of the view and returns None -- a TypeError and a 500.
  P2  tags_blocked_list's pagination links were copied from tags() and point at
      tag.tags, so page 2 of the banned list is page 2 of the unbanned one.
  P3  tag_posts filtered neither Community.private nor Post.private, so an
      anonymous request was served posts from invite-only communities.
  P4  community_id reached int(), and topic_id and feed_id reached .get()
      followed by an attribute access -- three crafted parameters, three 500s.
  P5  banning a tag flashed "and all content deleted" while purge_content() is
      commented out and every post survives.

Fact 292 applies: render_template is patched for anything that renders. Fact 293
applies to the ban pair, which is POST-only and needs a real CSRF token. Fact
322 applies throughout: one request per test, because flask_login answers a
second one as the first one's user.
"""
import pytest
from unittest.mock import patch

from flask import session
from flask_wtf.csrf import generate_csrf

from app import db
from app.models import Post, Site, Tag, post_tag
from tests.factories import (grant_permission, make_community, make_instance, make_post,
                             make_user)

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
    alice = make_user(instance, 'alice', local=True)
    bob = make_user(instance, 'bob', local=True)
    site = Site.query.get(1)
    site.private_instance = False
    db.session.commit()
    return instance, alice, bob


def _moderator(user):
    """A user `permission_required('manage users')` lets through, and that
    `trustworthy()` does not refuse: reputation below 100 on a recently created
    account is what tags_blocked_list turns into a 404.
    """
    grant_permission(user, 'manage users')
    user.reputation = 100
    db.session.commit()
    return user


def _tag(name='solarstorm', banned=False):
    tag = Tag(name=name.lower(), display_as=name, banned=banned, post_count=0)
    db.session.add(tag)
    db.session.commit()
    return tag


def _tagged(tag, community, author, title, **kwargs):
    post = make_post(community, author,
                     ap_id=f'https://test.piefed.local/post/{title.replace(" ", "")}',
                     title=title)
    for key, value in kwargs.items():
        setattr(post, key, value)
    db.session.execute(post_tag.insert().values(post_id=post.id, tag_id=tag.id))
    db.session.commit()
    return post


def _listed(render):
    return [tag.name for tag in render.call_args.kwargs['tags'].items]


def _posted(render):
    return [post.title for post in render.call_args.kwargs['posts']]


# --------------------------------------------------------------------------
# P1: a ban or unban naming no tag
# --------------------------------------------------------------------------


@pytest.mark.parametrize('action', ['ban', 'unban'])
def test_banning_a_tag_that_does_not_exist_is_refused_not_a_crash(app, db_session, action):
    """Before the repair, both halves of the pair fell off the end of the view:

        PROBE k2 exception: TypeError The view function for 'tag.tag_ban' did
        not return a valid response.

    A tag another moderator banned a moment earlier is the ordinary way to
    reach it.
    """
    instance, alice, bob = _seed()
    _moderator(alice)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/tag/nosuchtag/{action}', data={'csrf_token': token})

    assert response.status_code == 404


@pytest.mark.parametrize('action, banned_before, banned_after', [
    ('ban', False, True),
    ('unban', True, False),
])
def test_the_ban_pair_still_acts_on_a_tag_that_does_exist(app, db_session, action,
                                                          banned_before, banned_after):
    """The inversion of the row above: a repair that aborted unconditionally
    would pass it.
    """
    instance, alice, bob = _seed()
    _moderator(alice)
    tag = _tag(banned=banned_before)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/tag/solarstorm/{action}', data={'csrf_token': token})

    assert response.status_code == 302
    assert Tag.query.get(tag.id).banned is banned_after


# --------------------------------------------------------------------------
# P2: paging the banned list
# --------------------------------------------------------------------------


def test_paging_the_banned_list_stays_on_the_banned_list(app, db_session):
    """Before the repair:

        PROBE k3 next_url: /tags?page=2

    which is page 2 of a different list with different contents. An admin
    auditing bans past the hundredth read the unbanned tags and could not tell.
    """
    instance, alice, bob = _seed()
    _moderator(alice)
    for index in range(101):
        _tag(f'banned{index:03d}', banned=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get('/tags/banned')

    assert response.status_code == 200
    assert render.call_args.kwargs['next_url'].startswith('/tags/banned?')


def test_the_second_page_of_the_banned_list_holds_banned_tags(app, db_session):
    """The inversion: following the link has to reach banned tags, not merely a
    URL that starts with the right prefix.
    """
    instance, alice, bob = _seed()
    _moderator(alice)
    for index in range(101):
        _tag(f'banned{index:03d}', banned=True)
    _tag('notbanned')
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tags/banned')
        next_url = render.call_args.kwargs['next_url']
        client.get(next_url)

    assert _listed(render) == ['banned100']


def test_the_previous_link_of_the_banned_list_stays_on_it_too(app, db_session):
    instance, alice, bob = _seed()
    _moderator(alice)
    for index in range(101):
        _tag(f'banned{index:03d}', banned=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tags/banned?page=2')

    assert render.call_args.kwargs['prev_url'].startswith('/tags/banned?')


# --------------------------------------------------------------------------
# P3: the post list and private communities
# --------------------------------------------------------------------------


def test_an_anonymous_reader_is_not_served_a_private_communitys_posts(app, db_session):
    """Community.private is invite-only and is real access control
    (tests/README.md, "Three different tables have a `private` column").
    show_tag filters it; tag_posts, the same tag's posts for the same readers,
    did not:

        PROBE k4 status: 200
        PROBE k4 posts: ['post 0']
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _tagged(tag, community, alice, 'open post')
    _tagged(tag, private, alice, 'private post')
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert _posted(fl.render_template) == ['open post']


@pytest.mark.parametrize('a_member, expected', [
    (True, ['open post', 'private post']),
    (False, ['open post']),
])
def test_a_private_communitys_posts_reach_its_members_only(app, db_session, a_member,
                                                           expected):
    from tests.factories import make_community_member
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    private = make_community('privatecomm')
    private.private = True
    db.session.commit()
    _tagged(tag, community, alice, 'open post')
    _tagged(tag, private, alice, 'private post')
    if a_member:
        make_community_member(alice, private)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert sorted(_posted(fl.render_template)) == expected


def test_an_ingested_microblog_is_not_in_the_tags_post_list(app, db_session):
    """The other half of P3. show_tag filters Post.private; this route did not:

        PROBE k5 microblog posts: 1
        PROBE k5 show_tag posts: 0
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'an article')
    _tagged(tag, community, alice, 'a toot', private=True, microblog=True,
            body_html='<p>a short toot</p>')
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert _posted(fl.render_template) == ['an article']


# --------------------------------------------------------------------------
# P4: three crafted query parameters
# --------------------------------------------------------------------------


def test_a_community_id_that_is_not_a_number_narrows_nothing(app, db_session):
    """PROBE n3 exception: ValueError invalid literal for int() with base 10:
    'abc'. show_tag reads the same idea through `type=int` (:30); this one
    reached int() bare.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'a post')
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        response = client.get(f'/tags/posts/{tag.id}?community_id=abc')

    assert response.status_code == 200
    assert _posted(fl.render_template) == ['a post']


@pytest.mark.parametrize('parameter', ['topic_id', 'feed_id'])
def test_a_topic_or_feed_that_does_not_exist_is_a_404(app, db_session, parameter):
    """PROBE k7 / PROBE n4 exception: AttributeError 'NoneType' object has no
    attribute 'show_posts_in_children'. show_tag uses get_or_404 for both of
    these lookups (:67, :81).
    """
    instance, alice, bob = _seed()
    tag = _tag()
    client = app.test_client()

    response = client.get(f'/tags/posts/{tag.id}?{parameter}=9999')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# P5: the ban that says it deleted the content
# --------------------------------------------------------------------------


def test_banning_a_tag_does_not_claim_to_have_deleted_anything(app, db_session):
    """purge_content() is commented out at :224 and the flash was not:

        PROBE n2 tag banned: True
        PROBE n2 post still there: True

    A moderator who believes the posts are gone does not go and delete them.
    """
    from flask import get_flashed_messages
    instance, alice, bob = _seed()
    _moderator(alice)
    tag = _tag()
    community = make_community('microblogs')
    post = _tagged(tag, community, alice, 'a post')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with client:
        client.post('/tag/solarstorm/ban', data={'csrf_token': token})
        messages = get_flashed_messages()

    assert Post.query.get(post.id) is not None
    assert 'deleted' not in messages[0]
    assert 'solarstorm' in messages[0]


# --------------------------------------------------------------------------
# tags: the list of every tag
# --------------------------------------------------------------------------


def test_the_tag_list_leaves_out_banned_tags(app, db_session):
    instance, alice, bob = _seed()
    _tag('visible')
    _tag('hidden', banned=True)
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get('/tags')

    assert response.status_code == 200
    assert _listed(render) == ['visible']


def test_a_private_instance_refuses_an_anonymous_reader_the_tag_list(app, db_session):
    instance, alice, bob = _seed()
    site = Site.query.get(1)
    site.private_instance = True
    db.session.commit()
    client = app.test_client()

    response = client.get('/tags')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


@pytest.mark.parametrize('search, expected', [
    ('sol', ['solarstorm']),
    ('STORM', ['solarstorm']),
    ('', ['aurora', 'solarstorm']),
])
def test_the_tag_list_can_be_searched_without_regard_to_case(app, db_session, search,
                                                             expected):
    """`if search != ''` both ways, and `ilike` rather than `like`: a reader
    typing STORM is looking for the same tag as one typing storm.
    """
    instance, alice, bob = _seed()
    _tag('solarstorm')
    _tag('aurora')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags?search={search}')

    assert _listed(render) == expected
    assert render.call_args.kwargs['search'] == search


def test_the_tag_list_is_ordered_by_name(app, db_session):
    instance, alice, bob = _seed()
    for name in ['zephyr', 'aurora', 'solarstorm']:
        _tag(name)
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tags')

    assert _listed(render) == ['aurora', 'solarstorm', 'zephyr']


@pytest.mark.parametrize('reader, expected', [
    ('admin', True),
    ('ordinary', False),
    ('anonymous', False),
])
def test_only_staff_are_offered_the_ban_controls(app, db_session, reader, expected):
    """ban_visibility_permission is what the template hangs the ban button on.
    `current_user.is_authenticated and current_user.is_admin_or_staff()` is two
    operands, and the anonymous row is what makes the first load-bearing --
    is_admin_or_staff() does not exist on an anonymous user.
    """
    instance, alice, bob = _seed()
    _tag()
    client = app.test_client()
    if reader == 'admin':
        alice.roles.append(_admin_role())
        db.session.commit()
        login(client, alice)
    elif reader == 'ordinary':
        login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tags')

    assert render.call_args.kwargs['ban_visibility_permission'] is expected


def _admin_role():
    from app.models import Role
    role = Role.query.filter_by(name='Admin').first()
    if role is None:
        role = Role(name='Admin', weight=10)
        db.session.add(role)
        db.session.commit()
    return role


def test_the_tag_list_offers_links_only_when_there_is_another_page(app, db_session):
    instance, alice, bob = _seed()
    _tag('only')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tags')

    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['prev_url'] is None


def test_paging_the_tag_list(app, db_session):
    instance, alice, bob = _seed()
    for index in range(101):
        _tag(f'tag{index:03d}')
    client = app.test_client()

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tags')
        assert render.call_args.kwargs['next_url'].startswith('/tags?')
        assert render.call_args.kwargs['prev_url'] is None
        client.get('/tags?page=2')
        assert _listed(render) == ['tag100']
        assert render.call_args.kwargs['prev_url'].startswith('/tags?')
        assert render.call_args.kwargs['next_url'] is None


# --------------------------------------------------------------------------
# tags_blocked_list: the list of banned tags
# --------------------------------------------------------------------------


def test_the_banned_list_needs_a_login(app, db_session):
    instance, alice, bob = _seed()
    client = app.test_client()

    response = client.get('/tags/banned')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


def test_a_new_account_with_no_reputation_cannot_see_the_banned_list(app, db_session):
    """`trustworthy()` is False for an account created recently with reputation
    below 100 (app/models.py:1286), and the route turns that into a 404 rather
    than a refusal -- so the page does not admit it exists.
    """
    instance, alice, bob = _seed()
    _tag('hidden', banned=True)
    client = app.test_client()
    login(client, alice)

    response = client.get('/tags/banned')

    assert response.status_code == 404


def test_the_banned_list_shows_banned_tags_only(app, db_session):
    instance, alice, bob = _seed()
    _moderator(alice)
    _tag('visible')
    _tag('hidden', banned=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        response = client.get('/tags/banned')

    assert response.status_code == 200
    assert _listed(render) == ['hidden']


@pytest.mark.parametrize('search, expected', [
    ('storm', ['solarstorm']),
    ('', ['aurora', 'solarstorm']),
])
def test_the_banned_list_can_be_searched(app, db_session, search, expected):
    instance, alice, bob = _seed()
    _moderator(alice)
    _tag('solarstorm', banned=True)
    _tag('aurora', banned=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get(f'/tags/banned?search={search}')

    assert _listed(render) == expected
    assert render.call_args.kwargs['search'] == search


def test_the_banned_list_offers_no_links_when_everything_fits(app, db_session):
    instance, alice, bob = _seed()
    _moderator(alice)
    _tag('hidden', banned=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.render_template', return_value='rendered') as render:
        client.get('/tags/banned')

    assert render.call_args.kwargs['next_url'] is None
    assert render.call_args.kwargs['prev_url'] is None


# --------------------------------------------------------------------------
# the ban pair: the gates in front of them
# --------------------------------------------------------------------------


@pytest.mark.parametrize('action', ['ban', 'unban'])
def test_the_ban_pair_needs_a_login(app, db_session, action):
    instance, alice, bob = _seed()
    _tag()
    client = app.test_client()

    response = client.post(f'/tag/solarstorm/{action}')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']


@pytest.mark.parametrize('action', ['ban', 'unban'])
def test_a_reader_without_the_permission_cannot_ban_a_tag(app, db_session, action):
    instance, alice, bob = _seed()
    tag = _tag()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post(f'/tag/solarstorm/{action}', data={'csrf_token': token})

    # permission_required redirects to auth.permission_denied rather than
    # answering 401 (app/utils.py:1961); the tag is what proves the refusal.
    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']
    assert Tag.query.get(tag.id).banned is False


def test_the_tag_in_a_ban_url_is_matched_without_regard_to_case(app, db_session):
    instance, alice, bob = _seed()
    _moderator(alice)
    tag = _tag('SolarStorm')
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    client.post('/tag/SolarStorm/ban', data={'csrf_token': token})

    assert Tag.query.get(tag.id).banned is True


def test_an_unban_sends_the_moderator_to_the_tag_it_freed(app, db_session):
    """The two halves of the pair redirect to different places: a ban to the
    list, an unban to the tag that is readable again.
    """
    from flask import get_flashed_messages
    instance, alice, bob = _seed()
    _moderator(alice)
    _tag(banned=True)
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    with client:
        response = client.post('/tag/solarstorm/unban', data={'csrf_token': token})
        messages = get_flashed_messages()

    assert response.headers['Location'] == '/tag/solarstorm'
    assert 'solarstorm' in messages[0]


def test_a_ban_sends_the_moderator_back_to_the_list(app, db_session):
    instance, alice, bob = _seed()
    _moderator(alice)
    _tag()
    client = app.test_client()
    login(client, alice)
    token = csrf(app, client)

    response = client.post('/tag/solarstorm/ban', data={'csrf_token': token})

    assert response.headers['Location'] == '/tags'


# --------------------------------------------------------------------------
# tag_posts: the rest of it
# --------------------------------------------------------------------------


def test_the_post_list_leaves_out_bots_banned_communities_and_deleted_posts(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    banned = make_community('bannedcomm')
    banned.banned = True
    db.session.commit()
    _tagged(tag, community, alice, 'wanted post')
    _tagged(tag, community, alice, 'bot post', from_bot=True)
    _tagged(tag, community, alice, 'deleted post', deleted=True)
    _tagged(tag, community, alice, 'unreviewed post', status=0)
    _tagged(tag, banned, alice, 'banned post')
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert _posted(fl.render_template) == ['wanted post']


@pytest.mark.parametrize('ignore_bots, expected', [
    (1, ['human post']),
    (0, ['bot post', 'human post']),
])
def test_the_post_list_shows_bots_only_to_readers_who_want_them(app, db_session,
                                                                ignore_bots, expected):
    instance, alice, bob = _seed()
    alice.ignore_bots = ignore_bots
    db.session.commit()
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'human post')
    _tagged(tag, community, alice, 'bot post', from_bot=True)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert sorted(_posted(fl.render_template)) == expected


def test_a_logged_in_readers_own_blocks_apply_to_the_post_list(app, db_session):
    from tests.factories import (make_community_block, make_domain, make_domain_block,
                                 make_instance_block, make_user_block)
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    tag = _tag()
    community = make_community('microblogs')
    blocked_community = make_community('blockedcomm')
    domain = make_domain('blocked.test')
    _tagged(tag, community, alice, 'plain post')
    _tagged(tag, community, alice, 'domain post', domain_id=domain.id)
    _tagged(tag, community, alice, 'instance post', instance_id=peer.id)
    _tagged(tag, blocked_community, alice, 'community post')
    _tagged(tag, community, bob, 'blocked author post')
    make_domain_block(alice, domain)
    make_instance_block(alice, peer)
    make_community_block(alice, blocked_community)
    make_user_block(alice, bob)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert _posted(fl.render_template) == ['plain post']


def test_a_reader_who_has_blocked_nothing_sees_every_post(app, db_session):
    """The four `if <ids>:` guards from the other side, so none of them is
    load-bearing for the wrong reason.
    """
    from tests.factories import make_domain
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    tag = _tag()
    community = make_community('microblogs')
    domain = make_domain('open.test')
    _tagged(tag, community, alice, 'plain post')
    _tagged(tag, community, alice, 'domain post', domain_id=domain.id)
    _tagged(tag, community, alice, 'instance post', instance_id=peer.id)
    _tagged(tag, community, bob, 'other author post')
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert sorted(_posted(fl.render_template)) == ['domain post', 'instance post',
                                                   'other author post', 'plain post']


def test_a_post_with_no_instance_recorded_survives_an_instance_block(app, db_session):
    """The NULL half of `or_(Post.instance_id.not_in(...), Post.instance_id ==
    None)`, which D825 found unexercised in show_tag: SQL's NOT IN answers
    neither way for NULL, and make_post always records an instance.
    """
    from tests.factories import make_instance_block
    instance, alice, bob = _seed()
    peer = make_instance('remote.example', software='lemmy')
    tag = _tag()
    community = make_community('microblogs')
    _tagged(tag, community, alice, 'instance post', instance_id=peer.id)
    _tagged(tag, community, alice, 'instanceless post', instance_id=None)
    make_instance_block(alice, peer)
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert _posted(fl.render_template) == ['instanceless post']


@pytest.mark.parametrize('reads_language, expected', [
    (True, ['spoken post']),
    (False, ['spoken post', 'unspecified post']),
])
def test_a_reader_who_has_chosen_languages_sees_only_those(app, db_session,
                                                           reads_language, expected):
    from app.models import Language
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    language = Language(name='Spoken', code='sp')
    db.session.add(language)
    db.session.commit()
    _tagged(tag, community, alice, 'spoken post', language_id=language.id)
    _tagged(tag, community, alice, 'unspecified post')
    if reads_language:
        alice.read_language_ids = [language.id]
        db.session.commit()
    client = app.test_client()
    login(client, alice)

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    assert sorted(_posted(fl.render_template)) == expected


def test_a_community_id_narrows_the_post_list(app, db_session):
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    other = make_community('othercomm')
    _tagged(tag, community, alice, 'wanted post')
    _tagged(tag, other, alice, 'other post')
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}?community_id={community.id}')

    assert _posted(fl.render_template) == ['wanted post']


def _topic(name='fediverse', parent=None, show_posts_in_children=False):
    from app.models import Topic
    topic = Topic(name=name.title(), machine_name=name, num_communities=0,
                  parent_id=parent.id if parent else None,
                  show_posts_in_children=show_posts_in_children)
    db.session.add(topic)
    db.session.commit()
    return topic


def _in_topic(topic, name):
    community = make_community(name)
    community.topic_id = topic.id
    db.session.commit()
    return community


@pytest.mark.parametrize('show_posts_in_children, expected', [
    (False, ['parent post']),
    (True, ['child post', 'parent post']),
])
def test_a_topic_id_follows_the_topic_tree_only_when_asked(app, db_session,
                                                           show_posts_in_children, expected):
    instance, alice, bob = _seed()
    tag = _tag()
    parent = _topic('fediverse', show_posts_in_children=show_posts_in_children)
    child = _topic('microblogging', parent=parent)
    parent_community = _in_topic(parent, 'parentcomm')
    child_community = _in_topic(child, 'childcomm')
    elsewhere = make_community('elsewhere')
    _tagged(tag, parent_community, alice, 'parent post')
    _tagged(tag, child_community, alice, 'child post')
    _tagged(tag, elsewhere, alice, 'unrelated post')
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}?topic_id={parent.id}')

    assert sorted(_posted(fl.render_template)) == expected


@pytest.mark.parametrize('show_posts_in_children, expected', [
    (False, ['parent post']),
    (True, ['child post', 'parent post']),
])
def test_a_feed_id_follows_the_feed_tree_only_when_asked(app, db_session,
                                                         show_posts_in_children, expected):
    from tests.factories import make_feed_item, make_local_feed
    instance, alice, bob = _seed()
    tag = _tag()
    parent_feed = make_local_feed('parentfeed')
    parent_feed.show_posts_in_children = show_posts_in_children
    child_feed = make_local_feed('childfeed')
    child_feed.parent_feed_id = parent_feed.id
    db.session.commit()
    parent_community = make_community('parentcomm')
    child_community = make_community('childcomm')
    elsewhere = make_community('elsewhere')
    make_feed_item(parent_feed, parent_community)
    make_feed_item(child_feed, child_community)
    _tagged(tag, parent_community, alice, 'parent post')
    _tagged(tag, child_community, alice, 'child post')
    _tagged(tag, elsewhere, alice, 'unrelated post')
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}?feed_id={parent_feed.id}')

    assert sorted(_posted(fl.render_template)) == expected


def test_the_post_list_shows_the_newest_first_and_stops_at_seventy(app, db_session):
    from datetime import timedelta
    from app.models import utcnow
    instance, alice, bob = _seed()
    tag = _tag()
    community = make_community('microblogs')
    now = utcnow()
    for index in range(75):
        _tagged(tag, community, alice, f'post {index:02d}',
                posted_at=now - timedelta(minutes=75 - index))
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        client.get(f'/tags/posts/{tag.id}')

    titles = _posted(fl.render_template)
    assert len(titles) == 70
    assert titles[0] == 'post 74'
    assert 'post 00' not in titles


def test_the_post_list_is_rendered_from_its_own_template(app, db_session):
    """flask.render_template, not the theme-aware one -- this is an htmx
    fragment, so it must not be wrapped in a theme's base template.
    """
    instance, alice, bob = _seed()
    tag = _tag()
    client = app.test_client()

    with patch('app.tag.routes.flask') as fl:
        fl.render_template.return_value = 'rendered'
        response = client.get(f'/tags/posts/{tag.id}')

    assert response.status_code == 200
    assert fl.render_template.call_args.args[0] == 'tag/tag_posts.html'


def test_a_private_instance_refuses_an_anonymous_reader_the_post_list(app, db_session):
    instance, alice, bob = _seed()
    site = Site.query.get(1)
    site.private_instance = True
    db.session.commit()
    tag = _tag()
    client = app.test_client()

    response = client.get(f'/tags/posts/{tag.id}')

    assert response.status_code == 302
    assert '/auth/login' in response.headers['Location']
