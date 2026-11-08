"""A private community's comments are for its members, as its posts are.

/api/alpha refused a private community's post to a non-member (get_post) but answered its comments to anyone, signed
in or not, through both reply reads: /post/replies (get_post_replies) and /comment/list (get_reply_list). Found by the
API-completeness work's shared read layer (W0.1 step 3, divergence 2). The texts are the post listings' own: an
anonymous reader is asked to sign in, a signed-in non-member is told membership is required."""
import json
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.api.alpha.utils.post import get_post_replies
from app.api.alpha.utils.reply import get_reply_list
from app.models import Site
from tests.factories import make_community, make_community_member, make_post, make_post_reply, make_user

SECRET = 'members-only reply'
OPEN = 'everyone may read this reply'
ANONYMOUS = 'Private community - authentication required'
NOT_A_MEMBER = 'Private community - membership required'


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


@pytest.fixture
def world(app, api_baseline):
    """A private community with a post and a members-only reply, beside an open one; its member, and a reader who
    is a member of neither."""
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    private = make_community('hushhush')
    private.private = True
    public = make_community('townsquare')
    author = make_user(api_baseline.instance_local, 'insider', local=True)
    reader = make_user(api_baseline.instance_local, 'outsider', local=True)
    make_community_member(author, private)
    make_community_member(author, public)
    secret_post = make_post(private, author, ap_id='https://test.piefed.local/p/hush')
    open_post = make_post(public, author, ap_id='https://test.piefed.local/p/square')
    secret = make_post_reply(secret_post, author, body=SECRET)
    make_post_reply(open_post, author, body=OPEN)
    db.session.commit()
    return SimpleNamespace(private=private, author=author, reader=reader, secret_post=secret_post, secret=secret,
                           open_post=open_post)


def shown(answer) -> bool:
    return SECRET in json.dumps(answer, default=str)


READERS = [(None, ANONYMOUS), ('reader', NOT_A_MEMBER)]
READER_IDS = ['anonymous', 'non-member']


def auth_of(world, who):
    return None if who is None else token(getattr(world, who))


@pytest.mark.parametrize('who, refusal', READERS, ids=READER_IDS)
def test_the_post_replies_of_a_private_thread_are_refused(world, who, refusal):
    with pytest.raises(Exception, match=refusal):
        get_post_replies(auth_of(world, who), {'post_id': world.secret_post.id})


@pytest.mark.parametrize('who, refusal', READERS, ids=READER_IDS)
def test_a_branch_of_a_private_thread_is_refused(world, who, refusal):
    with pytest.raises(Exception, match=refusal):
        get_post_replies(auth_of(world, who), {'parent_id': world.secret.id})


@pytest.mark.parametrize('who, refusal', READERS, ids=READER_IDS)
@pytest.mark.parametrize('narrowing', ['post_id', 'parent_id', 'community_id'], ids=['post', 'parent', 'community'])
def test_a_comment_list_of_a_private_thread_or_community_is_refused(world, who, refusal, narrowing):
    target = {'post_id': world.secret_post.id, 'parent_id': world.secret.id, 'community_id': world.private.id}[narrowing]
    with pytest.raises(Exception, match=refusal):
        get_reply_list(auth_of(world, who), {narrowing: target})


@pytest.mark.parametrize('who', [None, 'reader'], ids=READER_IDS)
@pytest.mark.parametrize('narrowing', ['all', 'person'], ids=['all', 'person'])
def test_a_wider_comment_list_leaves_a_private_communitys_comments_out(world, who, narrowing):
    data = {'person_id': world.author.id} if narrowing == 'person' else {}
    answer = get_reply_list(auth_of(world, who), data)
    assert not shown(answer)
    assert OPEN in json.dumps(answer, default=str)   # the open community's comment is still there


def test_a_member_reads_the_thread_and_the_lists(world):
    member = token(world.author)
    assert shown(get_post_replies(member, {'post_id': world.secret_post.id}))
    assert shown(get_reply_list(member, {'post_id': world.secret_post.id}))
    assert shown(get_reply_list(member, {'community_id': world.private.id}))
    assert shown(get_reply_list(member, {}))


def test_a_member_sees_their_own_comment_on_their_own_profile(world):
    """The profile listing exempts the viewer from their own blocks, not from membership: a member still reads it."""
    assert shown(get_reply_list(token(world.author), {'person_id': world.author.id}))


def test_an_open_thread_is_unchanged_for_anyone(world):
    assert OPEN in json.dumps(get_post_replies(None, {'post_id': world.open_post.id}), default=str)
    assert OPEN in json.dumps(get_reply_list(None, {'post_id': world.open_post.id}), default=str)
