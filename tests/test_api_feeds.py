"""The feed API: listing feeds, reading one, following, creating, editing and
deleting.

Sub-project 84, slice B -- `app/api/alpha/utils/feed.py`. Two defects, both
measured:

* `post_feed_follow` asked nothing about who may see the feed. `get_feed`,
  one function above it, refuses a private feed to anybody but its owner --
  and `show_feed` (app/feed/routes.py:447) serves a private feed to anyone
  for whom `feed.subscribed(...)` is true, so following by id walked around
  the restriction entirely (D1173);
* `get_feed`'s `name` form split on `@` and took `parts[1]` without checking
  there was one: every bare feed name was `IndexError` (D1174).
"""
import pytest
from flask import g

from app import db
from app.models import Feed, FeedItem, FeedMember, Site, User
from tests.factories import (make_community, make_community_member,
                             make_instance, make_local_feed, make_user)


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def a_feed(owner, name='afeed', public=True, **columns):
    feed = make_local_feed(name, public=public)
    feed.user_id = owner.id
    feed.ap_domain = 'test.piefed.local'
    for column, value in columns.items():
        setattr(feed, column, value)
    db.session.commit()
    return feed


def in_feed(feed, community):
    db.session.add(FeedItem(feed_id=feed.id, community_id=community.id))
    feed.num_communities = (feed.num_communities or 0) + 1
    db.session.commit()
    return community


@pytest.fixture
def env(app, api_baseline):
    """`owner` has a public feed and a private one; `stranger` has neither,
    which is what D1173 is about."""
    g.admin_ids = []
    g.site = db.session.get(Site, 1)  # edit_feed reads it
    # `edit_feed` writes feed.nsfw only when the instance allows NSFW at all
    # (app/shared/feed.py:377), so a row about that flag has to enable it or
    # it is asserting on a write that never happens (fact 551).
    g.site.enable_nsfw = True
    g.site.enable_nsfl = True
    db.session.commit()
    owner = api_baseline.user2
    stranger = api_baseline.user3
    public = a_feed(owner, 'publicfeed', public=True)
    # A private feed is named `<url>/<owner>`: that is what `post_feed` writes
    # and what `edit_feed` renames to when a feed is made private, and
    # `feed_view` takes the part after the slash for a private feed's
    # actor_id. A bare name here would be a state the product cannot reach.
    private = a_feed(owner, f'secretfeed/{owner.user_name.lower()}',
                     public=False)
    return owner, stranger, public, private


# --------------------------------------------------------------------------
# D1173 -- following a feed nobody said you could see
# --------------------------------------------------------------------------


def test_a_stranger_may_not_read_a_private_feed(app, env):
    """The end that was guarded. Measured: `PROBE bb1 outcome: Exception:
    access_denied`."""
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        get_feed(token(stranger), {'id': private.id})

    assert str(refused.value) == 'access_denied'


def test_a_stranger_may_not_follow_a_private_feed(app, env):
    """D1173. The end that was not. `post_feed_follow` took the id and
    joined. Measured: `PROBE bb2 outcome: accepted | member now: True`."""
    from app.api.alpha.utils.feed import post_feed_follow

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        post_feed_follow(token(stranger), {'feed_id': private.id,
                                           'follow': True})

    assert str(refused.value) == 'access_denied'
    assert FeedMember.query.filter_by(user_id=stranger.id,
                                      feed_id=private.id).first() is None


def test_following_does_not_make_a_private_feed_readable(app, env):
    """What made D1173 more than an unwanted row: `show_feed` serves a
    private feed to anyone for whom `feed.subscribed(current_user.id)` is
    true. Measured: `PROBE bb7 subscribed before: 0 | after: 1`."""
    from app.api.alpha.utils.feed import post_feed_follow

    owner, stranger, public, private = env

    with pytest.raises(Exception):
        post_feed_follow(token(stranger), {'feed_id': private.id,
                                           'follow': True})

    assert private.subscribed(stranger.id) == 0


def test_the_owner_may_follow_their_own_private_feed(app, env):
    """The guard lets the owner through, or it has broken the thing it
    protects."""
    from app.api.alpha.utils.feed import post_feed_follow

    owner, stranger, public, private = env

    post_feed_follow(token(owner), {'feed_id': private.id, 'follow': True})

    assert private.subscribed(owner.id) != 0


def test_anybody_may_follow_a_public_feed(app, env):
    from app.api.alpha.utils.feed import post_feed_follow

    owner, stranger, public, private = env

    post_feed_follow(token(stranger), {'feed_id': public.id, 'follow': True})

    assert FeedMember.query.filter_by(user_id=stranger.id,
                                      feed_id=public.id).first() is not None


def test_somebody_may_always_leave_a_feed(app, env):
    """Leaving is not gated on being allowed to see it: a feed made private
    after somebody joined it must still be escapable."""
    from app.api.alpha.utils.feed import post_feed_follow

    owner, stranger, public, private = env
    post_feed_follow(token(stranger), {'feed_id': public.id, 'follow': True})
    # An UPDATE rather than an attribute write: `join_feed` commits inside the
    # call above, and an attribute set on the expired object afterwards did
    # not reach the row the endpoint then re-read (fact 552).
    db.session.query(Feed).filter_by(id=public.id).update({'public': False})
    db.session.commit()
    assert db.session.get(Feed, public.id).public is False

    post_feed_follow(token(stranger), {'feed_id': public.id, 'follow': False})

    assert FeedMember.query.filter_by(user_id=stranger.id,
                                      feed_id=public.id).first() is None


def test_following_a_feed_that_does_not_exist(app, env):
    from app.api.alpha.utils.feed import post_feed_follow

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        post_feed_follow(token(stranger), {'feed_id': 999999, 'follow': True})

    assert str(refused.value) == 'could not find feed'


# --------------------------------------------------------------------------
# D1174 -- reading one feed
# --------------------------------------------------------------------------


def test_a_feed_can_be_read_by_id(app, env):
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    answer = get_feed(token(stranger), {'id': public.id})

    assert answer['name'] == 'publicfeed'


def test_a_feed_can_be_read_by_name(app, env):
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    answer = get_feed(token(stranger), {'name': 'publicfeed@test.piefed.local'})

    assert answer['id'] == public.id


def test_a_bare_feed_name_is_a_bad_request_not_a_crash(app, env):
    """D1174. `parts[1]` on a name carrying no `@`. Measured: `PROBE bb4
    outcome: IndexError: list index out of range`."""
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        get_feed(token(stranger), {'name': 'publicfeed'})

    assert str(refused.value) == 'invalid_request'


def test_asking_for_no_feed_at_all_is_a_bad_request(app, env):
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        get_feed(token(stranger), {})

    assert str(refused.value) == 'invalid_request'


def test_a_feed_that_does_not_exist_is_not_found(app, env):
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        get_feed(token(stranger), {'id': 999999})

    assert str(refused.value) == 'feed_not_found'


def test_the_owner_reads_their_own_private_feed(app, env):
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    assert get_feed(token(owner), {'id': private.id})['id'] == private.id


def test_an_anonymous_reader_gets_the_public_feed(app, env):
    """`auth` is None for an unauthenticated call, and the blocked and
    subscribed lists are empty rather than looked up."""
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    assert get_feed(None, {'id': public.id})['id'] == public.id


def test_an_anonymous_reader_is_refused_a_private_feed(app, env):
    from app.api.alpha.utils.feed import get_feed

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        get_feed(None, {'id': private.id})

    assert str(refused.value) == 'access_denied'


# --------------------------------------------------------------------------
# The feed list
# --------------------------------------------------------------------------


def test_the_list_carries_the_public_feeds(app, env):
    from app.api.alpha.utils.feed import get_feed_list

    owner, stranger, public, private = env

    listed = get_feed_list(token(stranger), {})

    assert [f['name'] for f in listed['feeds']] == ['publicfeed']


def test_the_list_leaves_out_somebody_elses_private_feed(app, env):
    from app.api.alpha.utils.feed import get_feed_list

    owner, stranger, public, private = env

    listed = get_feed_list(token(stranger), {})

    assert 'secretfeed' not in ' '.join(f['name'] for f in listed['feeds'])


def test_mine_only_carries_my_own_feeds(app, env):
    from app.api.alpha.utils.feed import get_feed_list

    owner, stranger, public, private = env

    listed = get_feed_list(token(owner), {'mine_only': True})

    assert {f['name'] for f in listed['feeds']} == {
        'publicfeed', f'secretfeed/{owner.user_name.lower()}'}


def test_an_anonymous_reader_sees_the_public_list(app, env):
    from app.api.alpha.utils.feed import get_feed_list

    owner, stranger, public, private = env

    listed = get_feed_list(None, {})

    assert [f['name'] for f in listed['feeds']] == ['publicfeed']


def test_a_child_feed_is_nested_under_its_parent(app, env):
    from app.api.alpha.utils.feed import get_feed_list

    owner, stranger, public, private = env
    child = a_feed(owner, 'childfeed', public=True, parent_feed_id=public.id)

    listed = get_feed_list(token(stranger), {})

    parent = next(f for f in listed['feeds'] if f['name'] == 'publicfeed')
    assert [c['name'] for c in parent['children']] == ['childfeed']


def test_a_feed_with_no_children_carries_an_empty_list(app, env):
    from app.api.alpha.utils.feed import get_feed_list

    owner, stranger, public, private = env

    listed = get_feed_list(token(stranger), {})

    assert listed['feeds'][0]['children'] == []


def test_the_communities_can_be_left_out_of_the_list(app, env):
    from app.api.alpha.utils.feed import get_feed_list

    owner, stranger, public, private = env
    in_feed(public, make_community('general'))

    with_them = get_feed_list(token(stranger), {})
    without = get_feed_list(token(stranger), {'include_communities': False})

    assert with_them['feeds'][0]['communities'] != []
    assert without['feeds'][0].get('communities', []) == []


def test_a_blocked_community_is_left_out_of_a_feed(app, env):
    """The list is built for the reader: a community they have blocked is not
    part of what they are shown."""
    from unittest.mock import patch

    from app.api.alpha.utils.feed import get_feed_list

    owner, stranger, public, private = env
    community = in_feed(public, make_community('general'))

    with patch('app.api.alpha.utils.feed.blocked_communities',
               return_value=[community.id]):
        listed = get_feed_list(token(stranger), {})

    assert listed['feeds'][0]['communities'] == []


# --------------------------------------------------------------------------
# Creating, editing, deleting
# --------------------------------------------------------------------------


def test_a_feed_is_created(app, env):
    from app.api.alpha.utils.feed import post_feed

    owner, stranger, public, private = env

    answer = post_feed(token(stranger), {'name': 'A New Feed',
                                         'title': 'A New Feed'})

    assert answer['name'] == 'a_new_feed'
    assert answer['title'] == 'A New Feed'
    assert Feed.query.filter_by(name='a_new_feed').first().user_id == \
        stranger.id


def test_a_private_feed_is_named_after_its_owner(app, env):
    """A private feed's url carries the owner's name, so two people may each
    have one of the same name."""
    from app.api.alpha.utils.feed import post_feed

    owner, stranger, public, private = env

    post_feed(token(stranger), {'name': 'mine', 'title': 'Mine',
                                'public': False})

    assert Feed.query.filter_by(
        name=f'mine/{stranger.user_name.lower()}').first() is not None


def test_a_feed_is_created_with_what_it_was_given(app, env):
    from app.api.alpha.utils.feed import post_feed

    owner, stranger, public, private = env
    community = make_community('general')
    db.session.commit()

    answer = post_feed(token(stranger),
                       {'name': 'detailed', 'title': 'Detailed',
                        'description': 'about things', 'nsfw': True,
                        'nsfl': False, 'show_child_posts': True,
                        'communities': community.ap_id or community.name})

    assert answer['description'] == 'about things'
    assert answer['nsfw'] is True
    assert answer['show_posts_from_children'] is True


def test_a_feed_is_edited(app, env):
    from app.api.alpha.utils.feed import put_feed

    owner, stranger, public, private = env

    answer = put_feed(token(owner), {'feed_id': public.id,
                                     'title': 'A Better Title'})

    assert answer['title'] == 'A Better Title'


def test_editing_keeps_what_was_not_sent(app, env):
    """Every field defaults to what the feed already holds, so a partial
    update is not a silent reset."""
    from app.api.alpha.utils.feed import put_feed

    owner, stranger, public, private = env
    public.description = 'the original description'
    public.nsfw = True
    db.session.commit()

    answer = put_feed(token(owner), {'feed_id': public.id,
                                     'title': 'A Better Title'})

    assert answer['description'] == 'the original description'
    assert answer['nsfw'] is True


def test_editing_keeps_the_communities_that_were_not_sent(app, env):
    """The community list is rebuilt from the feed's own items when the
    caller does not send one -- otherwise a title change would empty it."""
    from app.api.alpha.utils.feed import put_feed

    owner, stranger, public, private = env
    community = in_feed(public, make_community('general'))

    put_feed(token(owner), {'feed_id': public.id, 'title': 'A Better Title'})

    assert FeedItem.query.filter_by(feed_id=public.id,
                                    community_id=community.id).first() \
        is not None


def test_a_stranger_may_not_edit_a_feed(app, env):
    """The ownership check lives in `edit_feed`; this endpoint has none of
    its own, which is what that check's comment records."""
    from app.api.alpha.utils.feed import put_feed

    owner, stranger, public, private = env

    with pytest.raises(Exception):
        put_feed(token(stranger), {'feed_id': public.id, 'title': 'mine now'})

    assert public.title == 'publicfeed'


def test_editing_a_feed_that_does_not_exist(app, env):
    from app.api.alpha.utils.feed import put_feed

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        put_feed(token(owner), {'feed_id': 999999, 'title': 'x'})

    assert str(refused.value) == 'not_found'


def test_a_feed_is_deleted(app, env):
    from app.api.alpha.utils.feed import post_feed_delete

    owner, stranger, public, private = env

    answer = post_feed_delete(token(owner), {'feed_id': public.id,
                                             'deleted': True})

    assert answer['id'] == public.id
    assert db.session.get(Feed, public.id) is None


def test_a_delete_that_says_false_deletes_nothing(app, env):
    from app.api.alpha.utils.feed import post_feed_delete

    owner, stranger, public, private = env

    post_feed_delete(token(owner), {'feed_id': public.id, 'deleted': False})

    assert db.session.get(Feed, public.id) is not None


def test_a_stranger_may_not_delete_a_feed(app, env):
    from app.api.alpha.utils.feed import post_feed_delete

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        post_feed_delete(token(stranger), {'feed_id': public.id,
                                           'deleted': True})

    assert str(refused.value) == 'not_found'
    assert db.session.get(Feed, public.id) is not None


def test_deleting_a_feed_that_does_not_exist(app, env):
    from app.api.alpha.utils.feed import post_feed_delete

    owner, stranger, public, private = env

    with pytest.raises(Exception) as refused:
        post_feed_delete(token(owner), {'feed_id': 999999, 'deleted': True})

    assert str(refused.value) == 'not_found'


def test_editing_replaces_the_communities_that_were_sent(app, env):
    """The other arm: a caller who names communities gets exactly those."""
    from app.api.alpha.utils.feed import put_feed

    owner, stranger, public, private = env
    was_in = in_feed(public, make_community('general'))
    now_in = make_community('elsewhere')
    db.session.commit()

    put_feed(token(owner), {'feed_id': public.id,
                            'communities': now_in.ap_id or now_in.name})

    assert FeedItem.query.filter_by(feed_id=public.id,
                                    community_id=was_in.id).first() is None
    assert FeedItem.query.filter_by(feed_id=public.id,
                                    community_id=now_in.id).first() is not None
