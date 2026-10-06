"""The search, resolve-object and suggestion endpoints.

Sub-project 84, slice F -- `app/api/alpha/utils/misc.py` without the modlog.
Two defects, both measured:

* `get_search`'s guard was `'q' not in data and 'type_' not in data`, so
  either key satisfied it -- and `data['type_']` is read on the next line, so
  a search carrying only `q` was `KeyError: 'type_'` (D1189);
* `get_resolve_object`'s local dispatch put `('/comment/' not in query)` at
  the end of an `or` chain, and `and` binds tighter, so the exclusion applied
  to the third disjunct alone. A comment url that names its community --
  which is the shape PieFed's own permalinks take -- resolved as the
  COMMUNITY (D1190).
"""
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.models import BannedInstances, Community, Feed, Post, PostReply, Site, User
from tests.factories import (make_community, make_local_feed, make_post,
                             make_post_reply, make_user)


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def local_url(path):
    return f"https://{current_app.config['SERVER_NAME']}{path}"


@pytest.fixture
def env(app, api_baseline):
    """Everything here is addressed by url, so each row's actor needs an
    `ap_profile_id` on this instance."""
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = api_baseline.user2
    author = api_baseline.user3
    community = make_community('general')
    community.ap_profile_id = local_url('/c/general')
    community.ap_id = None
    post = api_baseline.post1
    post.ap_id = local_url(f'/p/{post.id}')
    reply = make_post_reply(post, author, body='a reply')
    reply.body_html = '<p>a reply</p>'
    reply.ap_id = local_url(f'/comment/{reply.id}')
    db.session.commit()
    return user, author, community, post, reply, api_baseline


# --------------------------------------------------------------------------
# D1189 -- the search guard
# --------------------------------------------------------------------------


def test_a_search_needs_both_a_query_and_a_type(app, env):
    """D1189. The guard asked for `q` OR `type_` and the body reads `type_`
    on the next line. Measured: `PROBE bj1 outcome: KeyError: 'type_'`."""
    from app.api.alpha.utils.misc import get_search

    user, author, community, post, reply, baseline = env

    for data in ({'q': 'anything'}, {'type_': 'Posts'}, {}, None):
        with pytest.raises(Exception) as refused:
            get_search(token(user), data)
        assert str(refused.value) == 'missing parameters for search'


@pytest.mark.parametrize('type_,key', [('Communities', 'communities'),
                                       ('Posts', 'posts'),
                                       ('Url', 'posts'),
                                       ('Users', 'users'),
                                       ('Comments', 'comments')])
def test_a_search_answers_under_the_key_for_its_type(app, env, type_, key):
    from app.api.alpha.utils.misc import get_search

    user, author, community, post, reply, baseline = env

    answer = get_search(token(user), {'q': '', 'type_': type_})

    assert key in answer
    assert answer['type_'] == ('Posts' if type_ == 'Url' else type_)


def test_the_listing_type_is_passed_on_as_the_type(app, env):
    """`data['type_'] = listing_type` before the list is fetched: the list
    endpoints read `type_` as the LISTING type, not the search type."""
    from app.api.alpha.utils.misc import get_search

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.get_post_list',
               return_value={'posts': []}) as listed:
        get_search(token(user), {'q': 'x', 'type_': 'Posts',
                                 'listing_type': 'Local'})

    assert listed.call_args.args[1]['type_'] == 'Local'


def test_a_search_finds_a_post(app, env):
    from app.api.alpha.utils.misc import get_search

    user, author, community, post, reply, baseline = env

    answer = get_search(token(user), {'q': post.title, 'type_': 'Posts'})

    assert post.id in [p['post']['id'] for p in answer['posts']]


# --------------------------------------------------------------------------
# D1190 -- a comment url that names its community
# --------------------------------------------------------------------------


def test_a_comment_url_that_names_its_community_resolves_to_the_comment(app,
                                                                        env):
    """D1190. `and` binds tighter than `or`, so `('/comment/' not in query)`
    applied to the third disjunct alone and this url was dispatched as a
    community lookup. Measured: `PROBE bj2 outcome: ['community']`."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    answer = get_resolve_object(token(user),
                                {'q': local_url(f'/c/general/comment/{reply.id}')})

    assert 'comment' in answer
    assert answer['comment']['comment']['id'] == reply.id


def test_a_community_url_still_resolves_to_the_community(app, env):
    """The guard the fix must not break."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    answer = get_resolve_object(token(user), {'q': local_url('/c/general')})

    assert answer['community']['community']['id'] == community.id


def test_a_comment_url_without_a_community_resolves_too(app, env):
    """Measured alongside D1190 as `PROBE bj3 outcome: ['comment']`."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    answer = get_resolve_object(
        token(user), {'q': local_url(f'/post/{post.id}/comment/{reply.id}')})

    assert answer['comment']['comment']['id'] == reply.id


# --------------------------------------------------------------------------
# What is already here
# --------------------------------------------------------------------------


def test_a_query_that_is_already_a_known_comment(app, env):
    """The first lookup: the canonical ap_id of something this instance has."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    answer = get_resolve_object(token(user), {'q': reply.ap_id})

    assert answer['comment']['comment']['id'] == reply.id


def test_a_deleted_comment_is_not_found(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    reply.deleted = True
    db.session.commit()

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': reply.ap_id})

    assert str(refused.value) == 'No object found.'


def test_a_query_that_is_already_a_known_post(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    answer = get_resolve_object(token(user), {'q': post.ap_id})

    assert answer['post']['post']['id'] == post.id


def test_a_deleted_post_is_not_found(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    post.deleted = True
    db.session.commit()

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': post.ap_id})

    assert str(refused.value) == 'No object found.'


def test_a_banned_community_is_not_found(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    community.banned = True
    db.session.commit()

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': community.ap_profile_id})

    assert str(refused.value) == 'No object found.'


def test_a_query_that_is_already_a_known_account(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    author.ap_profile_id = local_url(f'/u/{author.user_name}')
    db.session.commit()

    answer = get_resolve_object(token(user), {'q': author.ap_profile_id})

    assert answer['person']['person']['id'] == author.id


@pytest.mark.parametrize('column', ['deleted', 'banned'])
def test_an_account_that_is_gone_is_not_found(app, env, column):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    author.ap_profile_id = local_url(f'/u/{author.user_name}')
    setattr(author, column, True)
    db.session.commit()

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': author.ap_profile_id})

    assert str(refused.value) == 'No object found.'


def test_a_query_that_is_already_a_known_feed(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.ap_profile_id = local_url('/f/newsfeed')
    feed.user_id = user.id
    db.session.commit()

    answer = get_resolve_object(token(user), {'q': feed.ap_profile_id})

    assert answer['feed']['id'] == feed.id


def test_a_banned_feed_is_not_found(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.ap_profile_id = local_url('/f/newsfeed')
    feed.user_id = user.id
    feed.banned = True
    db.session.commit()

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': feed.ap_profile_id})

    assert str(refused.value) == 'No object found.'


# --------------------------------------------------------------------------
# Who may ask, and about what
# --------------------------------------------------------------------------


def test_a_resolve_needs_something_to_resolve(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    for data in ({}, None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), data)
        assert 'missing q parameter' in str(refused.value)


def test_a_query_that_names_no_server_is_not_found(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': 'just some words'})

    assert str(refused.value) == 'No object found.'


@pytest.mark.parametrize('remote', [True, False])
def test_an_anonymous_caller_may_not_resolve_anything(app, env, remote):
    """PERM-1, fixed (owner ruling). Resolving can fetch and store remote
    content, so it requires an authenticated caller: with no Authorization
    header the API's standard `incorrect_login` refusal (a 400) is raised,
    for a local lookup as well as a remote one. Anonymous callers used to be
    allowed through, the only credential-free path to `create_resolved_object`
    in the permission audit."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_resolve_object(None, {'q': 'https://remote.example/c/general' if remote
                                  else local_url('/c/general')})

    assert str(refused.value) == 'incorrect_login'


def test_a_banned_instance_is_not_resolved_from(app, env):
    """A server this instance has banned is not fetched from, however the
    query is addressed."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    db.session.add(BannedInstances(domain='nasty.example'))
    db.session.commit()

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': 'https://nasty.example/c/x'})

    assert str(refused.value) == 'No object found.'


# --------------------------------------------------------------------------
# The notations
# --------------------------------------------------------------------------


def test_a_community_in_bang_notation(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    name = f"!general@{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_community',
               return_value=community) as searched:
        answer = get_resolve_object(token(user), {'q': name})

    assert answer['community']['community']['id'] == community.id
    assert searched.call_args.args[0] == name


def test_a_person_in_at_notation(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    name = f"@{author.user_name}@{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_user',
               return_value=author) as searched:
        answer = get_resolve_object(token(user), {'q': name})

    assert answer['person']['person']['id'] == author.id
    assert searched.call_args.args[0] == author.user_name.lower()


def test_a_feed_in_tilde_notation(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.user_id = user.id
    db.session.commit()
    name = f"~newsfeed@{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_feed', return_value=feed):
        answer = get_resolve_object(token(user), {'q': name})

    assert answer['feed']['id'] == feed.id


def test_a_name_nobody_answers_to_is_not_found(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    name = f"!nosuch@{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_community',
               return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': name})

    assert str(refused.value) == 'No object found.'


def test_a_local_post_by_its_url(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    answer = get_resolve_object(token(user),
                                {'q': local_url(f'/post/{post.id}')})

    assert answer['post']['post']['id'] == post.id


@pytest.mark.parametrize('prefix', ['/post/', '/p/', '/t/'])
def test_every_post_url_shape_this_instance_knows(app, env, prefix):
    """Lemmy writes /post/, recent PieFed /p/, mbin /t/."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    answer = get_resolve_object(token(user),
                                {'q': local_url(f'{prefix}{post.id}')})

    assert answer['post']['post']['id'] == post.id


def test_a_post_url_with_no_id_in_it(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': local_url('/post/')})

    assert str(refused.value) == 'No object found.'


def test_a_post_that_is_not_here(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': local_url('/post/999999')})

    assert str(refused.value) == 'No object found.'


def test_a_comment_that_is_not_here(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': local_url('/comment/999999')})

    assert str(refused.value) == 'No object found.'


def test_a_local_feed_by_its_url(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.user_id = user.id
    db.session.commit()

    with patch('app.api.alpha.utils.misc.search_for_feed', return_value=feed):
        answer = get_resolve_object(token(user), {'q': local_url('/f/newsfeed')})

    assert answer['feed']['id'] == feed.id


def test_a_feed_url_that_names_no_feed(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.search_for_feed', return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': local_url('/f/nosuch')})

    assert str(refused.value) == 'No object found.'


def test_a_community_url_that_names_no_community(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.search_for_community',
               return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': local_url('/c/nosuch')})

    assert str(refused.value) == 'No object found.'


def test_a_local_account_by_its_url(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.search_for_user',
               return_value=author) as searched:
        answer = get_resolve_object(token(user),
                                    {'q': local_url(f'/u/{author.user_name}')})

    assert answer['person']['person']['id'] == author.id


def test_a_user_url_that_names_nobody(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.search_for_user', return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': local_url('/u/nosuch')})

    assert str(refused.value) == 'No object found.'


# --------------------------------------------------------------------------
# Suggestions
# --------------------------------------------------------------------------


def test_a_person_suggestion_prefers_people_in_the_thread(app, env):
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env

    answer = get_suggestion({'q': f'@{author.user_name[:3]}',
                             'post_id': post.id})

    assert author.lemmy_link() in answer['result']


def test_a_person_suggestion_without_a_thread(app, env):
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env

    answer = get_suggestion({'q': f'@{author.user_name[:3]}'})

    assert author.lemmy_link() in answer['result']


def test_a_person_suggestion_falls_back_to_a_looser_match(app, env):
    """The prefix search first, then `%name%` if seven have not been found."""
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env
    somebody = make_user(baseline.instance_local, 'xx_findme_xx', local=True)
    db.session.commit()

    answer = get_suggestion({'q': '@findme'})

    assert somebody.lemmy_link() in answer['result']


def test_a_person_suggestion_stops_at_seven(app, env):
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env
    for number in range(9):
        make_user(baseline.instance_local, f'samename{number}', local=True)
    db.session.commit()

    answer = get_suggestion({'q': '@samename'})

    assert len(answer['result']) == 7


def test_a_community_suggestion(app, env):
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env

    answer = get_suggestion({'q': '!gen'})

    assert community.lemmy_link()[1:] in answer['result']


def test_a_suggestion_about_neither(app, env):
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env

    assert get_suggestion({'q': 'general'}) == {'result': []}


# --------------------------------------------------------------------------
# Reaching off the instance
# --------------------------------------------------------------------------


def remote(name='faraway', profile_id=None):
    """A community on another server, as the fetch would leave it.

    `profile_id` defaults to something the query will NOT match: the first
    lookup in `get_resolve_object` is `filter_by(ap_profile_id=query)`, so a
    community whose profile id equals the url under test is answered there
    and the fetch path is never reached.
    """
    community = make_community(name, host='remote.example')
    community.ap_id = f'{name}@remote.example'
    community.ap_profile_id = profile_id or f'https://remote.example/other/{name}'
    db.session.commit()
    return community


def test_a_remote_community_already_federated_is_answered_locally(app, env):
    """`search_for_community(..., allow_fetch=False)` decides whether this is
    a local lookup: something already federated is not fetched again."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = remote(profile_id='https://remote.example/c/faraway')

    with patch('app.api.alpha.utils.misc.search_for_community',
               return_value=theirs):
        answer = get_resolve_object(token(user), {'q': '!faraway@remote.example'})

    assert answer['community']['community']['id'] == theirs.id


def test_a_remote_person_already_federated(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = make_user(baseline.instance_remote, 'faraway')
    db.session.commit()

    with patch('app.api.alpha.utils.misc.search_for_user', return_value=theirs):
        answer = get_resolve_object(token(user),
                                    {'q': '@faraway@remote.example'})

    assert answer['person']['person']['id'] == theirs.id


def test_a_remote_feed_already_federated(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.user_id = user.id
    db.session.commit()

    with patch('app.api.alpha.utils.misc.search_for_feed', return_value=feed):
        answer = get_resolve_object(token(user), {'q': '~newsfeed@remote.example'})

    assert answer['feed']['id'] == feed.id


def test_a_url_hint_sends_a_community_to_the_actor_fetch(app, env):
    """`/c/` in a url with no local match: `find_actor_or_create` is what
    goes and asks the other server."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = remote()

    with patch('app.api.alpha.utils.misc.find_actor_or_create',
               return_value=theirs) as fetched:
        answer = get_resolve_object(token(user),
                                    {'q': 'https://remote.example/c/faraway'})

    assert answer['community']['community']['id'] == theirs.id
    fetched.assert_called_once()


def test_a_url_hint_sends_a_person_to_the_actor_fetch(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = make_user(baseline.instance_remote, 'faraway')
    db.session.commit()

    with patch('app.api.alpha.utils.misc.find_actor_or_create',
               return_value=theirs):
        answer = get_resolve_object(token(user),
                                    {'q': 'https://remote.example/u/faraway'})

    assert answer['person']['person']['id'] == theirs.id


def test_a_remote_feed_document_becomes_a_feed(app, env):
    """A feed url carries no `/u/`, `/c/` or `/m/` hint, so it goes to the
    document fetch rather than the actor fetch."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.user_id = user.id
    feed.ap_profile_id = 'https://remote.example/other/newsfeed'
    db.session.commit()

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/f/newsfeed',
                             'type': 'Feed', 'preferredUsername': 'Newsfeed'}):
        with patch('app.api.alpha.utils.misc.actor_json_to_model',
                   return_value=feed):
            answer = get_resolve_object(token(user),
                                        {'q': 'https://remote.example/f/newsfeed'})

    assert answer['feed']['id'] == feed.id


def test_a_document_the_other_server_will_not_hand_over(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user),
                               {'q': 'https://remote.example/thing/1'})

    assert str(refused.value) == 'No object found.'


@pytest.mark.parametrize('document', [{}, {'type': 'Note'},
                                      {'id': 'https://remote.example/x'}])
def test_a_document_missing_what_it_needs(app, env, document):
    """No `id`, or an `id` that matches and no `type`."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value=document):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert str(refused.value) == 'No object found.'


def test_a_document_that_names_another_url_is_followed(app, env):
    """"query URL doesn't match original author's URL" -- the canonical id
    wins, and the function calls itself with it."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': post.ap_id}):
        answer = get_resolve_object(token(user),
                                    {'q': 'https://remote.example/thing/1'})

    assert answer['post']['post']['id'] == post.id


@pytest.mark.parametrize('actor_type', ['Person', 'Service', 'Group'])
def test_an_actor_document_becomes_an_actor(app, env, actor_type):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = make_user(baseline.instance_remote, f'faraway{actor_type}')
    db.session.commit()

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x',
                             'type': actor_type,
                             'preferredUsername': 'Faraway'}):
        with patch('app.api.alpha.utils.misc.actor_json_to_model',
                   return_value=theirs) as built:
            answer = get_resolve_object(token(user),
                                        {'q': 'https://remote.example/x'})

    assert answer['person']['person']['id'] == theirs.id
    assert built.call_args.args[1] == 'faraway'


def test_an_actor_document_that_is_a_community(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = remote()

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Group',
                             'preferredUsername': 'Faraway'}):
        with patch('app.api.alpha.utils.misc.actor_json_to_model',
                   return_value=theirs):
            answer = get_resolve_object(token(user),
                                        {'q': 'https://remote.example/x'})

    assert answer['community']['community']['id'] == theirs.id


def test_a_post_document_is_created_in_its_community(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = remote()

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Page'}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=theirs):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       return_value=post) as created:
                answer = get_resolve_object(token(user),
                                            {'q': 'https://remote.example/x'})

    assert answer['post']['post']['id'] == post.id
    assert created.call_args.args[3] is theirs


def test_a_comment_document_is_created_too(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = remote()

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Note'}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=theirs):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       return_value=reply):
                answer = get_resolve_object(token(user),
                                            {'q': 'https://remote.example/x'})

    assert answer['comment']['comment']['id'] == reply.id


def test_a_document_whose_community_is_named_in_its_audience(app, env):
    """With no community known, the audience/cc/to fields are resolved in
    turn until one of them answers with a community."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Page',
                             'audience': community.ap_profile_id}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=None):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       return_value=post) as created:
                get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert created.call_args.args[3].id == community.id


def test_a_document_whose_community_is_in_a_list(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Page',
                             'cc': ['https://www.w3.org/ns#Public',
                                    community.ap_profile_id]}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=None):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       return_value=post) as created:
                get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert created.call_args.args[3].id == community.id


def test_a_reply_takes_its_community_from_what_it_answers(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Note',
                             'inReplyTo': post.ap_id}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=None):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       return_value=reply) as created:
                get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert created.call_args.args[3].id == post.community_id


def test_a_reply_to_a_comment_takes_its_community_too(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Note',
                             'inReplyTo': reply.ap_id}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=None):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       return_value=reply) as created:
                get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert created.call_args.args[3].id == reply.community_id


def test_a_document_with_no_community_anywhere(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Page'}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=None):
            with pytest.raises(Exception) as refused:
                get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert str(refused.value) == 'No object found.'


def test_a_document_that_cannot_be_created_at_all(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = remote()

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Page'}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=theirs):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       return_value=None):
                with pytest.raises(Exception) as refused:
                    get_resolve_object(token(user),
                                       {'q': 'https://remote.example/x'})

    assert str(refused.value) == 'No object found.'


def test_a_reply_whose_parent_is_fetched_first(app, env):
    """"if object can't be created due to missing a parent post or reply" --
    the parent is resolved and the creation tried once more."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = remote()

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Note',
                             'inReplyTo': post.ap_id}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=theirs):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       side_effect=[None, reply]) as created:
                answer = get_resolve_object(token(user),
                                            {'q': 'https://remote.example/x'})

    assert answer['comment']['comment']['id'] == reply.id
    assert created.call_count == 2


def test_an_actor_document_with_no_name_is_not_found(app, env):
    """D1191. `type == 'Person' or ... or type == 'Feed' and
    'preferredUsername' in ap_json` -- `and` binds tighter, so the membership
    test guarded the Feed arm alone while the line below read the key for all
    four types. Measured: `PROBE bk1 outcome: KeyError:
    'preferredUsername'`. D1190's shape, second instance in this file."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x',
                             'type': 'Person'}):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert str(refused.value) == 'No object found.'


# --------------------------------------------------------------------------
# The many ways a query resolves to nothing
# --------------------------------------------------------------------------


def test_a_local_person_addressed_with_this_instances_domain(app, env):
    """`@name@this-instance` is trimmed to the bare name before the lookup,
    on both the local-request test and the search itself."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    name = f"@{author.user_name}@{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_user',
               return_value=author) as searched:
        get_resolve_object(token(user), {'q': name})

    assert all(call.args[0] == author.user_name.lower()
               for call in searched.call_args_list)


def test_a_community_url_with_no_name_in_it(app, env):
    """The `/c/` regex matches nothing, so there is no name to look up."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.search_for_community', return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': local_url('/m/')})

    assert str(refused.value) == 'No object found.'


def test_a_community_url_carrying_its_domain(app, env):
    """A name that already names its instance is not given ours as well."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.search_for_community',
               return_value=community) as searched:
        get_resolve_object(token(user),
                           {'q': local_url('/c/general@elsewhere.example')})

    assert searched.call_args.args[0] == '!general@elsewhere.example'


def test_a_person_url_carrying_this_instances_domain(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    name = f"{author.user_name}@{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_user',
               return_value=author) as searched:
        get_resolve_object(token(user), {'q': local_url(f'/u/{name}')})

    assert searched.call_args.args[0] == author.user_name.lower()


def test_a_person_url_with_no_name_in_it(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.search_for_user', return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': local_url('/u/')})

    assert str(refused.value) == 'No object found.'


def test_a_feed_url_with_no_name_in_it(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.search_for_feed', return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': local_url('/f/')})

    assert str(refused.value) == 'No object found.'


def test_a_feed_in_tilde_notation_that_names_no_feed(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    name = f"~nosuch@{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_feed', return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': name})

    assert str(refused.value) == 'No object found.'


def test_a_feed_url_carrying_its_domain(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.user_id = user.id
    db.session.commit()

    with patch('app.api.alpha.utils.misc.search_for_feed',
               return_value=feed) as searched:
        get_resolve_object(token(user),
                           {'q': local_url('/f/newsfeed@elsewhere.example')})

    assert searched.call_args.args[0] == '~newsfeed@elsewhere.example'


def test_a_bang_query_with_no_local_match_is_fetched(app, env):
    """The hint block below the local one: `!name@server` that this instance
    does not already have goes out to fetch it."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = remote()

    with patch('app.api.alpha.utils.misc.search_for_community',
               side_effect=[None, theirs]):
        answer = get_resolve_object(token(user), {'q': '!faraway@remote.example'})

    assert answer['community']['community']['id'] == theirs.id


def test_an_at_query_with_no_local_match_is_fetched(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    theirs = make_user(baseline.instance_remote, 'faraway')
    db.session.commit()

    with patch('app.api.alpha.utils.misc.search_for_user',
               side_effect=[None, theirs]):
        answer = get_resolve_object(token(user), {'q': '@faraway@remote.example'})

    assert answer['person']['person']['id'] == theirs.id


def test_a_tilde_query_with_no_local_match_is_fetched(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.user_id = user.id
    feed.ap_profile_id = 'https://remote.example/other/newsfeed'
    db.session.commit()

    with patch('app.api.alpha.utils.misc.search_for_feed',
               side_effect=[None, feed]):
        answer = get_resolve_object(token(user), {'q': '~newsfeed@remote.example'})

    assert answer['feed']['id'] == feed.id


def test_an_actor_fetch_that_answers_a_feed(app, env):
    """`/m/` without `/t/` is the mbin community shape, and the fetch can
    answer with any of the three actor kinds."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.user_id = user.id
    db.session.commit()

    with patch('app.api.alpha.utils.misc.find_actor_or_create',
               return_value=feed):
        answer = get_resolve_object(token(user),
                                    {'q': 'https://remote.example/m/newsfeed'})

    assert answer['feed']['id'] == feed.id


def test_an_actor_fetch_that_finds_nothing_falls_through(app, env):
    """`find_actor_or_create` answering None is not the end: the document
    fetch below it still runs."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.find_actor_or_create',
               return_value=None):
        with patch('app.api.alpha.utils.misc.remote_object_to_json',
                   return_value=None):
            with pytest.raises(Exception) as refused:
                get_resolve_object(token(user),
                                   {'q': 'https://remote.example/c/faraway'})

    assert str(refused.value) == 'No object found.'


def test_a_reply_whose_parent_is_itself_fetched(app, env):
    """The parent is not here, so it is resolved recursively and its
    community used."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    def fetched(query):
        if query == 'https://remote.example/x':
            return {'id': query, 'type': 'Note',
                    'inReplyTo': 'https://remote.example/parent'}
        return {'id': 'https://remote.example/parent', 'type': 'Note'}

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               side_effect=fetched):
        with patch('app.api.alpha.utils.misc.find_community',
                   side_effect=[None, community]):
            with patch('app.api.alpha.utils.misc.create_resolved_object',
                       return_value=reply):
                answer = get_resolve_object(token(user),
                                            {'q': 'https://remote.example/x'})

    assert answer['comment']['comment']['id'] == reply.id


def test_a_suggestion_for_a_name_nobody_has(app, env):
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env

    assert get_suggestion({'q': '@nobodyatall'}) == {'result': []}


def test_an_anonymous_feed_lookup_carries_empty_lists(app, env):
    """`feed_view_arguments` without a caller: nothing is subscribed, nothing
    is blocked, and no database lookup is made for any of it."""
    from app.api.alpha.utils.misc import feed_view_arguments

    user, author, community, post, reply, baseline = env

    arguments = feed_view_arguments(None)

    assert arguments['user_id'] is None
    assert arguments['subscribed'] == []
    assert arguments['blocked_instance_ids'] == []


def test_a_bare_name_with_no_server_in_it(app, env):
    """`!name` with no `@server`: there is no instance to ask."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': '!general'})

    assert str(refused.value) == 'No object found.'


def test_a_post_url_the_regex_cannot_read(app, env):
    """`/p/` with a non-numeric id: the pattern matches nothing."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': local_url('/p/notanumber')})

    assert str(refused.value) == 'No object found.'


def test_a_comment_url_the_regex_cannot_read(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        get_resolve_object(token(user), {'q': local_url('/comment/notanumber')})

    assert str(refused.value) == 'No object found.'


def test_a_local_url_that_is_none_of_the_shapes(app, env):
    """A url on this instance naming nothing the dispatch recognises falls
    past every branch."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': local_url('/something/else')})

    assert str(refused.value) == 'No object found.'


def test_a_local_feed_already_known_by_its_profile_id(app, env):
    """The early lookup answers a feed without the query having to look like
    one -- which is what D1192 was about."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.ap_profile_id = local_url('/other/newsfeed')
    feed.user_id = user.id
    db.session.commit()

    answer = get_resolve_object(token(user), {'q': feed.ap_profile_id})

    assert answer['feed']['id'] == feed.id


def test_a_document_whose_audience_names_nothing_useful(app, env):
    """The w3 namespace and a followers collection are both skipped."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': 'https://remote.example/x', 'type': 'Page',
                             'to': ['https://www.w3.org/ns#Public',
                                    'https://remote.example/c/x/followers'],
                             'audience': 'https://www.w3.org/ns#Public'}):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=None):
            with pytest.raises(Exception) as refused:
                get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert str(refused.value) == 'No object found.'


def test_a_reply_whose_parent_cannot_be_resolved_either(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    def fetched(query):
        if query == 'https://remote.example/x':
            return {'id': query, 'type': 'Note',
                    'inReplyTo': 'https://remote.example/parent'}
        return None

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               side_effect=fetched):
        with patch('app.api.alpha.utils.misc.find_community',
                   return_value=None):
            with pytest.raises(Exception) as refused:
                get_resolve_object(token(user), {'q': 'https://remote.example/x'})

    assert str(refused.value) == 'No object found.'


def test_a_suggestion_does_not_repeat_somebody(app, env):
    """Somebody who replied to the post and also matches the looser search is
    listed once."""
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env

    answer = get_suggestion({'q': f'@{author.user_name}', 'post_id': post.id})

    assert answer['result'].count(author.lemmy_link()) == 1


def test_a_remote_comment_url_that_names_its_community(app, env):
    """D1190's twin, on the path that reaches off the instance: the same
    precedence bug decided whether a remote comment permalink was fetched as
    an actor."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.find_actor_or_create') as fetched:
        with patch('app.api.alpha.utils.misc.remote_object_to_json',
                   return_value=None):
            with pytest.raises(Exception):
                get_resolve_object(
                    token(user),
                    {'q': 'https://remote.example/c/faraway/comment/7'})

    fetched.assert_not_called()


def test_a_bare_feed_url_is_given_this_instances_domain(app, env):
    """`/f/newsfeed` with no `@` names a feed here, and the lookup is made
    with the full address."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    feed = make_local_feed('newsfeed', public=True)
    feed.user_id = user.id
    # Not the url under test: the early `filter_by(ap_profile_id=query)`
    # answers before the dispatch this row is about is reached.
    feed.ap_profile_id = local_url('/other/newsfeed')
    db.session.commit()

    with patch('app.api.alpha.utils.misc.search_for_feed',
               return_value=feed) as searched:
        get_resolve_object(token(user), {'q': local_url('/f/newsfeed')})

    assert searched.call_args.args[0] == \
        f"~newsfeed@{current_app.config['SERVER_NAME']}"


def test_seven_suggestions_even_when_the_thread_fills_them(app, env):
    """The cap is checked inside the loop, not only by the query's limit: the
    people who replied to the post are added first."""
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env
    # Seven from the thread fills the list, so the loop below it breaks on
    # its first iteration rather than running out of candidates.
    for number in range(7):
        somebody = make_user(baseline.instance_local, f'samename{number}',
                             local=True)
        make_post_reply(post, somebody, body=f'reply {number}')
    for number in range(7, 12):
        make_user(baseline.instance_local, f'samename{number}', local=True)
    db.session.commit()

    answer = get_suggestion({'q': '@samename', 'post_id': post.id})

    assert len(answer['result']) == 7


def test_a_lookalike_domain_is_not_this_instance(app, env):
    """`@name@sub.<server>` ends with our domain but is not it, so the
    at-notation branch trims the name before asking whether it is here."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    name = f"@{author.user_name}@sub.{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_user',
               return_value=author) as searched:
        answer = get_resolve_object(token(user), {'q': name})

    assert answer['person']['person']['id'] == author.id
    assert searched.call_args_list[0].args[0] == author.user_name.lower()


def test_seven_suggestions_from_the_looser_search_too(app, env):
    """The cap is checked in the second loop as well, which only runs when
    the first one left room."""
    from app.api.alpha.utils.misc import get_suggestion

    user, author, community, post, reply, baseline = env
    # Six whose name STARTS with the query, so the prefix loop leaves room
    # for exactly one more -- and two that only the `%name%` search finds,
    # given the reputation to sort FIRST in it. The second loop then reaches
    # seven on its first candidate and breaks on its second.
    for number in range(6):
        make_user(baseline.instance_local, f'findme{number}', local=True)
    for number in range(2):
        somebody = make_user(baseline.instance_local, f'xx_findme{number}',
                             local=True)
        somebody.reputation = 100
    db.session.commit()

    answer = get_suggestion({'q': '@findme'})

    assert len(answer['result']) == 7


def test_an_at_query_that_names_nobody_here(app, env):
    """The at-notation branch of a local request: the name is ours to
    resolve, and nobody answers to it."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env
    name = f"@nosuch@{current_app.config['SERVER_NAME']}"

    with patch('app.api.alpha.utils.misc.search_for_user',
               return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': name})

    assert str(refused.value) == 'No object found.'


# --------------------------------------------------------------------------
# Coverage floor: the remaining branches of get_resolve_object, _own_host_author
# and get_suggestion
# --------------------------------------------------------------------------

REMOTE_X = 'https://remote.example/x'


def resolve_document(user, document, *, community=None, **patches):
    """Resolve REMOTE_X as `document`, with find_community answering `community`; any other name in
    app.api.alpha.utils.misc can be replaced through `patches`."""
    from app.api.alpha.utils.misc import get_resolve_object

    patches.setdefault('remote_object_to_json', lambda query: document)
    patches.setdefault('find_community', lambda *a, **k: community)
    with patch.multiple('app.api.alpha.utils.misc', **patches):
        return get_resolve_object(token(user), {'q': REMOTE_X})


@pytest.mark.parametrize('attributed_to', [
    {'id': 'https://remote.example/users/author', 'type': 'Person'},
    [{'id': 'https://remote.example/users/author', 'type': 'Person'}],
    ['https://remote.example/users/author'],
])
def test_an_object_attributed_to_an_actor_on_its_own_host_names_that_author(app, attributed_to):
    """The attributedTo may be one actor object, a list of them or a list of ids; D24 R2 hands the author on."""
    from app.api.alpha.utils.misc import _own_host_author

    assert _own_host_author({'id': REMOTE_X, 'attributedTo': attributed_to}) == 'https://remote.example/users/author'


def test_an_author_on_another_host_is_not_the_object_s_author(app):
    from app.api.alpha.utils.misc import _own_host_author

    assert _own_host_author({'id': REMOTE_X, 'attributedTo': {'id': 'https://elsewhere.example/u/a',
                                                              'type': 'Person'}}) is None


def test_a_banned_podcasts_episode_is_dropped(app, env):
    """D24: a top-level episode whose podcast twin is banned is not resolved into microblogs."""
    user, author, community, post, reply, baseline = env
    from app.models import Instance
    instance = Instance.query.filter_by(domain='remote.example').first()
    if instance is None:
        instance = Instance(domain='remote.example')
        db.session.add(instance)
        db.session.commit()
    podcast = make_user(instance, 'thepodcast')
    twin = make_community('thepodcast', host='remote.example')
    twin.ap_profile_id = podcast.ap_profile_id
    twin.banned = True
    db.session.commit()

    with pytest.raises(Exception) as refused:
        resolve_document(user, {'id': REMOTE_X, 'type': 'Note', 'attributedTo': podcast.ap_profile_id})

    assert str(refused.value) == 'No object found.'


def test_an_audience_that_is_not_a_community_is_not_taken_for_one(app, env):
    """The audience resolves to a post here, so it cannot supply the community; with no other source the
    document is not found."""
    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        resolve_document(user, {'id': REMOTE_X, 'type': 'Page', 'audience': post.ap_id})

    assert str(refused.value) == 'No object found.'


def test_a_parent_that_resolves_to_nothing_gives_no_community(app, env):
    """The parent is resolved recursively; when that answers nothing the reply has no community."""
    from app.api.alpha.utils import misc

    user, author, community, post, reply, baseline = env
    real = misc.get_resolve_object

    def only_the_outer_call(auth, data, user_id=None, recursive=False):
        return None if recursive else real(auth, data, user_id, recursive)

    with patch.object(misc, 'get_resolve_object', only_the_outer_call):
        with pytest.raises(Exception) as refused:
            resolve_document(user, {'id': REMOTE_X, 'type': 'Note',
                                    'inReplyTo': 'https://remote.example/parent'})

    assert str(refused.value) == 'No object found.'


def test_an_object_the_caller_may_not_view_is_not_found(app, env):
    user, author, community, post, reply, baseline = env

    with pytest.raises(Exception) as refused:
        resolve_document(user, {'id': REMOTE_X, 'type': 'Page'}, community=community,
                         create_resolved_object=lambda *a, **k: post, can_view=lambda *a, **k: False)

    assert str(refused.value) == 'No object found.'


def test_a_created_object_that_is_neither_post_nor_reply_is_not_found(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.remote_object_to_json',
               return_value={'id': REMOTE_X, 'type': 'Page'}), \
            patch('app.api.alpha.utils.misc.find_community', return_value=community), \
            patch('app.api.alpha.utils.misc.create_resolved_object', return_value=object()):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': REMOTE_X}, recursive=True)

    assert str(refused.value) == 'No object found.'


@pytest.mark.parametrize('query,finder', [
    ('!faraway@remote.example', 'search_for_community'),
    ('@someone@remote.example', 'search_for_user'),
    ('~somefeed@remote.example', 'search_for_feed'),
])
def test_a_hint_that_finds_nothing_falls_through_to_the_fetch(app, env, query, finder):
    """A !, @ or ~ query nobody answers to goes on to the actor fetch and the document fetch."""
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch(f'app.api.alpha.utils.misc.{finder}', return_value=None), \
            patch('app.api.alpha.utils.misc.find_actor_or_create', return_value=None) as fetched, \
            patch('app.api.alpha.utils.misc.remote_object_to_json', return_value=None) as asked:
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': query})

    assert str(refused.value) == 'No object found.'
    asked.assert_called_once()


def test_an_actor_fetch_that_answers_something_unexpected_falls_through(app, env):
    from app.api.alpha.utils.misc import get_resolve_object

    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.find_actor_or_create', return_value=object()), \
            patch('app.api.alpha.utils.misc.remote_object_to_json', return_value=None):
        with pytest.raises(Exception) as refused:
            get_resolve_object(token(user), {'q': 'https://remote.example/c/faraway'})

    assert str(refused.value) == 'No object found.'


@pytest.mark.parametrize('model', [None, object()])
def test_an_actor_document_that_makes_no_known_actor_falls_through(app, env, model):
    """actor_json_to_model answering nothing (or something that is not an actor) leaves a Group document with
    no community to be posted into."""
    user, author, community, post, reply, baseline = env

    with patch('app.api.alpha.utils.misc.actor_json_to_model', return_value=model):
        with pytest.raises(Exception) as refused:
            resolve_document(user, {'id': REMOTE_X, 'type': 'Group', 'preferredUsername': 'Faraway'})

    assert str(refused.value) == 'No object found.'


def test_a_suggestion_lists_a_person_once(app, env):
    """Two accounts that print as the same handle are one suggestion, not two."""
    from app.api.alpha.utils.misc import get_suggestion
    from app.models import Instance

    user, author, community, post, reply, baseline = env
    instance = Instance.query.filter_by(domain='remote.example').first()
    if instance is None:
        instance = Instance(domain='remote.example')
        db.session.add(instance)
        db.session.commit()
    twins = [make_user(instance, 'dupname'), make_user(instance, 'dupname2')]
    for twin in twins:
        twin.ap_id = 'dupname@remote.example'
        make_post_reply(post, twin, body='hi')
    db.session.commit()

    answer = get_suggestion({'q': '@dupname', 'post_id': post.id})

    assert answer['result'].count('dupname@remote.example') == 1
