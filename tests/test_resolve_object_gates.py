"""Follow-up F5: every way `resolve_object` can return a post, reply, community, user or feed applies the viewer gate
the matching page applies, and a thing the caller may not see is answered exactly as one that does not exist."""
import pytest
from flask import g

from app import db
import app.api.alpha.utils.misc as misc
from app.api.alpha.utils.misc import get_resolve_object
from app.constants import POST_STATUS_DRAFT, VISIBILITY_FOLLOWERS
from app.models import CommunityMember, Post, Site
from app.storefront import domain_for_host
from tests.factories import make_instance, make_local_feed, make_post, make_post_reply, make_user
from tests.test_local_only_invisible import _post, world  # noqa: F401
from tests.two_domains import request_on

pytestmark = pytest.mark.usefixtures('site')

MISSING = 'https://peer.example/post/424242'


@pytest.fixture(autouse=True)
def api_globals(app, db_session):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)


def caller(name='carol'):
    user = make_user(None, name, local=True)
    user.verified = True
    db.session.commit()
    return user


def resolve(user, url, recursive=False):
    return get_resolve_object(None, {'q': url}, user_id=user.id, recursive=recursive)


def refused(user, url, recursive=False):
    with pytest.raises(Exception) as caught:
        resolve(user, url, recursive)
    return str(caught.value)


def peer_post(world, community=None):
    """A post from another server, stored in one of our communities, as the inbox leaves it."""
    peer = make_instance('peer.example')
    author = make_user(peer, 'pat')
    post = make_post(community or world.clearnet, author, 'https://peer.example/post/1')
    db.session.commit()
    return post


def peer_reply(post):
    reply = make_post_reply(post, post.author)
    reply.ap_id = 'https://peer.example/comment/1'
    db.session.commit()
    return reply


def _followers_only(post, world):
    post.visibility = VISIBILITY_FOLLOWERS


def _draft(post, world):
    post.status = POST_STATUS_DRAFT


def _deleted(post, world):
    post.deleted = True
    post.deleted_by = world.alice.id


def _banned_community(post, world):
    post.community.banned = True


def _private_community(post, world):
    post.community.private = True


HIDDEN = [_followers_only, _draft, _deleted, _banned_community, _private_community]


# ---- a post from another server, by its stored ap_id --------------------------------------------------------------

def test_a_visible_peer_post_resolves_by_its_ap_id(world):
    post = peer_post(world)
    assert resolve(caller(), post.ap_id)['post']['post']['id'] == post.id


@pytest.mark.parametrize('hide', HIDDEN)
def test_a_peer_post_the_caller_may_not_open_is_not_found(world, hide):
    post = peer_post(world)
    hide(post, world)
    db.session.commit()
    assert refused(caller(), post.ap_id) == 'No object found.'


def test_a_peer_post_in_a_private_community_resolves_for_a_member(world):
    post = peer_post(world)
    post.community.private = True
    user = caller()
    db.session.add(CommunityMember(user_id=user.id, community_id=post.community_id))
    db.session.commit()
    assert resolve(user, post.ap_id)['post']['post']['id'] == post.id


# ---- deleted posts: the stored and the local branch answer as the post page does ----------------------------------

def _urls(app, post):
    return [post.ap_id, f"https://{app.config['SERVER_NAME']}/post/{post.id}"]


def test_a_post_deleted_by_a_moderator_is_gone_on_both_branches_for_a_reader(app, world):
    post = peer_post(world)
    post.deleted = True
    post.deleted_by = world.alice.id
    db.session.commit()
    user = caller()
    assert [refused(user, url) for url in _urls(app, post)] == ['No object found.'] * 2


def test_a_post_deleted_by_a_moderator_is_still_shown_to_a_moderator_on_both_branches(app, world):
    post = peer_post(world)
    post.deleted = True
    post.deleted_by = world.alice.id
    db.session.commit()
    assert [resolve(world.alice, url)['post']['post']['id'] for url in _urls(app, post)] == [post.id] * 2


def test_a_post_its_author_deleted_is_shown_on_both_branches_as_the_post_page_shows_it(app, world):
    post = peer_post(world)
    post.deleted = True
    post.deleted_by = post.user_id
    db.session.commit()
    user = caller()
    assert [resolve(user, url)['post']['post']['id'] for url in _urls(app, post)] == [post.id] * 2


# ---- recursive=True ------------------------------------------------------------------------------------------------

@pytest.mark.parametrize('hide', HIDDEN)
def test_a_recursive_call_is_gated_too(world, hide):
    post = peer_post(world)
    hide(post, world)
    db.session.commit()
    assert refused(caller(), post.ap_id, recursive=True) == 'No object found.'


def test_a_recursive_call_returns_the_object_for_a_visible_post(world):
    post = peer_post(world)
    assert resolve(caller(), post.ap_id, recursive=True) == post
    assert isinstance(post, Post)


def test_a_recursive_call_for_a_private_community_is_refused_to_a_non_member(world):
    world.clearnet.private = True
    db.session.commit()
    assert refused(caller(), world.clearnet.ap_profile_id, recursive=True) == 'No object found.'


# ---- replies --------------------------------------------------------------------------------------------------------

def test_a_visible_peer_reply_resolves(world):
    reply = peer_reply(peer_post(world))
    assert resolve(caller(), reply.ap_id)['comment']['comment']['id'] == reply.id


@pytest.mark.parametrize('hide', [_draft, _banned_community, _private_community, _deleted])
def test_a_reply_under_a_post_the_caller_may_not_open_is_not_found(app, world, hide):
    post = peer_post(world)
    reply = peer_reply(post)
    hide(post, world)
    db.session.commit()
    user = caller()
    local = f"https://{app.config['SERVER_NAME']}/comment/{reply.id}"
    assert [refused(user, url) for url in (reply.ap_id, local)] == ['No object found.'] * 2


def test_a_deleted_reply_is_not_found_on_both_branches(app, world):
    reply = peer_reply(peer_post(world))
    reply.deleted = True
    db.session.commit()
    user = caller()
    local = f"https://{app.config['SERVER_NAME']}/comment/{reply.id}"
    assert [refused(user, url) for url in (reply.ap_id, local)] == ['No object found.'] * 2


# ---- communities ----------------------------------------------------------------------------------------------------

def test_a_visible_community_resolves_by_every_route(app, world):
    user = caller()
    for url in (world.clearnet.ap_profile_id, f"!clearnet@{app.config['SERVER_NAME']}"):
        assert resolve(user, url)['community']['community']['id'] == world.clearnet.id


def test_a_private_community_resolves_for_a_member(world):
    world.clearnet.private = True
    user = caller()
    db.session.add(CommunityMember(user_id=user.id, community_id=world.clearnet.id))
    db.session.commit()
    assert resolve(user, world.clearnet.ap_profile_id)['community']['community']['id'] == world.clearnet.id


@pytest.mark.parametrize('state', ['banned', 'private'])
def test_a_community_the_page_would_refuse_is_not_found_by_every_route(app, world, state):
    setattr(world.clearnet, state, True)
    db.session.commit()
    user = caller()
    urls = [world.clearnet.ap_profile_id, f"!clearnet@{app.config['SERVER_NAME']}",
            f"https://{app.config['SERVER_NAME']}/c/clearnet/"]
    assert [refused(user, url) for url in urls] == ['No object found.'] * 3


def test_a_local_only_community_is_open_to_a_logged_in_reader_and_not_to_a_logged_out_one(app, world):
    """On the onion; off it, to nobody (follow-up F6)."""
    with request_on(app, domain_for_host('abc.onion')):
        assert misc._community_open_to(world.hidden, 1) and not misc._community_open_to(world.hidden, None)
    assert not misc._community_open_to(world.hidden, 1)
    assert misc._community_open_to(world.clearnet, None)


# ---- users ----------------------------------------------------------------------------------------------------------

def test_a_visible_user_resolves_by_every_route(app, world):
    user = caller()
    for url in (world.bob.ap_profile_id or f"https://{app.config['SERVER_NAME']}/u/bob",
                f"@bob@{app.config['SERVER_NAME']}", f"https://{app.config['SERVER_NAME']}/u/bob"):
        assert resolve(user, url)['person']['person']['id'] == world.bob.id


@pytest.mark.parametrize('state', ['banned', 'deleted'])
def test_a_deleted_or_banned_user_is_not_found_by_every_route(app, world, state):
    setattr(world.bob, state, True)
    db.session.commit()
    user = caller()
    urls = [f"@bob@{app.config['SERVER_NAME']}", f"https://{app.config['SERVER_NAME']}/u/bob"]
    assert [refused(user, url) for url in urls] == ['No object found.'] * 2


def test_a_local_only_account_is_open_to_a_logged_in_reader_and_not_to_a_logged_out_one(app, world):
    """On the onion; off it, to nobody (follow-up F6)."""
    with request_on(app, domain_for_host('abc.onion')):
        assert misc._user_open_to(world.onionite, 1) and not misc._user_open_to(world.onionite, None)
    assert not misc._user_open_to(world.onionite, 1)
    assert misc._user_open_to(world.bob, None)


# ---- feeds ----------------------------------------------------------------------------------------------------------

def feed_url(feed):
    return feed.ap_profile_id


def test_a_public_feed_resolves(world):
    feed = make_local_feed('open', public=True)
    assert resolve(caller(), feed_url(feed))['feed']['id'] == feed.id


def test_a_private_feed_is_not_found_to_anyone_but_its_owner_and_an_admin(world):
    feed = make_local_feed('closed', public=False)
    owner = caller('olive')
    feed.user_id = owner.id
    db.session.commit()
    assert refused(caller(), feed_url(feed)) == 'No object found.'
    assert resolve(owner, feed_url(feed))['feed']['id'] == feed.id
    assert resolve(world.alice, feed_url(feed))['feed']['id'] == feed.id


def test_a_banned_feed_is_not_found(world):
    feed = make_local_feed('gone', public=True)
    feed.banned = True
    db.session.commit()
    assert refused(caller(), feed_url(feed)) == 'No object found.'


# ---- no oracle --------------------------------------------------------------------------------------------------------

def _get(app, user, url):
    return app.test_client().get('/api/alpha/resolve_object', query_string={'q': url},
                                 headers={'Authorization': f'Bearer {user.encode_jwt_token()}'})


@pytest.mark.parametrize('hide', HIDDEN)
def test_a_hidden_post_is_answered_byte_for_byte_as_a_missing_one(app, world, monkeypatch, hide):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    post = peer_post(world)
    hide(post, world)
    db.session.commit()
    user = caller()
    hidden = _get(app, user, post.ap_id)
    missing = _get(app, user, f"https://{app.config['SERVER_NAME']}/post/424242")
    assert hidden.status_code == missing.status_code == 400
    assert hidden.get_data() == missing.get_data()
    assert b'No object found.' in hidden.get_data()


def test_a_private_feed_is_answered_as_a_missing_one(app, world, monkeypatch):
    monkeypatch.setitem(app.config, 'ENABLE_ALPHA_API', 'true')
    feed = make_local_feed('closed', public=False)
    user = caller()
    hidden = _get(app, user, feed_url(feed))
    missing = _get(app, user, f"https://{app.config['SERVER_NAME']}/post/424242")
    assert hidden.status_code == missing.status_code and hidden.get_data() == missing.get_data()


# ---- the protocol handler ------------------------------------------------------------------------------------------

@pytest.mark.parametrize('hide', HIDDEN)
def test_the_protocol_handler_does_not_open_a_peer_post_the_viewer_may_not_open(app, world, hide):
    from tests.test_hosted_post_resolve import handler
    post = peer_post(world)
    hide(post, world)
    db.session.commit()
    response = handler(app, caller(), post.ap_id.replace('https://', 'web+ap://'))
    assert response.status_code == 302 and not response.headers['Location'].endswith(f'/post/{post.id}')


def test_the_protocol_handler_opens_a_visible_peer_post(app, world):
    from tests.test_hosted_post_resolve import handler
    post = peer_post(world)
    response = handler(app, caller(), post.ap_id.replace('https://', 'web+ap://'))
    assert response.status_code == 302 and response.headers['Location'].endswith(f'/post/{post.id}')


def test_a_reply_under_a_followers_only_post_still_resolves(app, world):
    post = peer_post(world)
    reply = peer_reply(post)
    post.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    assert resolve(caller(), reply.ap_id)['comment']['comment']['id'] == reply.id


# ---- a parent fetched on the way: only the child's own gates decide (follow-up F5) -------------------------------

PARENT = 'https://peer.example/post/9'
CHILD = 'https://peer.example/comment/9'
MENTIONED = 'https://peer.example/u/mia'
MENTIONED2 = 'https://peer.example/u/nobody'


class FakePeer:
    """The peer's documents, and the stored rows its fetches leave. `community_known` is whether `find_community`
    names the child's community without the parent."""

    def __init__(self, world, hide, community_known):
        self.world, self.hide, self.community_known = world, hide, community_known
        self.fetched = []
        self.instance = make_instance('peer.example')
        db.session.commit()

    def remote_object_to_json(self, url, **kwargs):
        self.fetched.append(url)
        return {'id': url, 'type': 'Page' if url == PARENT else 'Note',
                **({'inReplyTo': PARENT, 'cc': [MENTIONED2, MENTIONED]} if url == CHILD else {})}

    def find_actor_or_create(self, url, **kwargs):
        self.fetched.append(url)
        return None

    def find_community(self, ap_json, **kwargs):
        return self.world.clearnet if ap_json['id'] == PARENT or self.community_known else None

    def create_resolved_object(self, query, ap_json, server, community, announce_id, store_ap_json):
        if query == PARENT:
            post = make_post(self.world.clearnet, make_user(self.instance, 'pat'), PARENT)
            self.hide(post, self.world)
            db.session.commit()
            return post
        parent = Post.get_by_ap_id(PARENT)
        if parent is None:
            return None
        reply = make_post_reply(parent, parent.author)
        reply.ap_id = CHILD
        db.session.commit()
        return reply


@pytest.fixture
def peer(world, monkeypatch):
    def install(hide=_followers_only, community_known=False):
        fake = FakePeer(world, hide, community_known)
        for name in ('remote_object_to_json', 'find_actor_or_create', 'find_community', 'create_resolved_object'):
            monkeypatch.setattr(misc, name, getattr(fake, name))
        return fake
    return install


@pytest.mark.parametrize('community_known', [False, True])
def test_a_reply_resolves_when_its_fetched_parent_is_followers_only(peer, community_known):
    fake = peer(_followers_only, community_known)
    answer = resolve(caller(), CHILD)
    assert answer['comment']['comment']['id'] == Post.get_by_ap_id(PARENT).replies[0].id
    assert PARENT in fake.fetched


@pytest.mark.parametrize('community_known', [False, True])
@pytest.mark.parametrize('hide', [_draft, _banned_community, _private_community, _deleted])
def test_a_reply_whose_fetched_parent_the_caller_may_not_open_is_not_found(peer, hide, community_known):
    peer(hide, community_known)
    assert refused(caller(), CHILD) == 'No object found.'


def test_a_banned_account_the_reply_mentions_does_not_stop_it_resolving(world, peer):
    fake = peer(_followers_only, community_known=False)
    mia = make_user(fake.instance, 'mia')
    mia.ap_profile_id = MENTIONED
    mia.banned = True
    db.session.commit()
    assert resolve(caller(), CHILD)['comment']['comment']['id']


def test_the_not_found_text_is_one_shared_value():
    import inspect
    assert inspect.getsource(misc).count("'No object found.'") == 1 and misc.NOT_FOUND == 'No object found.'
