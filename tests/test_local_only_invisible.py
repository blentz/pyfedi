"""Local-only accounts, communities and content do not exist to other servers (plan B task 8).

What another server can ask for, it is answered 404: the ActivityPub documents and collections, WebFinger, and the
NodeInfo counts leave them out. A local-only account cannot be made a moderator of a federating community.
"""
from datetime import datetime
from types import SimpleNamespace

import pytest

from app import db
from app.constants import SRC_API
from app.models import CommunityMember, PostBoost, PostReplyVote, PostVote, SiteDomain, UserFollower, utcnow
from app.shared import community as shared_community
from app.storefront import clear_domain_cache
from tests.factories import make_community, make_feed_item, make_instance, make_local_feed, make_post, make_post_reply, \
    make_user
from tests.test_community_lifecycle import csrf, login, url

pytestmark = pytest.mark.usefixtures('site')

AP = {'Accept': 'application/activity+json'}


@pytest.fixture
def world(app, db_session):
    """hell.cloud (the default domain) and an onion that does not federate, each with an account and a community.

    alice and bob are hell.cloud accounts; onionite is the onion's. `clearnet` is hell.cloud's community, `hidden`
    the onion's. alice owns both communities."""
    make_instance(app.config['SERVER_NAME'])   # the local instance, id 1
    default = db.session.query(SiteDomain).filter_by(is_default=True).first()
    if default is None:
        default = SiteDomain(domain=app.config['SERVER_NAME'], is_default=True)
        db.session.add(default)
    onion = SiteDomain(domain='abc.onion', scheme='http', federates=False)
    db.session.add(onion)
    db.session.commit()
    clear_domain_cache()
    alice = make_user(None, 'alice', local=True, with_keys=True)
    bob = make_user(None, 'bob', local=True)
    onionite = make_user(None, 'onionite', local=True, with_keys=True)
    alice.site_domain_id = default.id
    onionite.site_domain_id = onion.id
    onionite.ap_profile_id = onionite.ap_public_url = 'http://abc.onion/u/onionite'
    for user in (alice, bob, onionite):
        user.last_seen = utcnow()
    clearnet = make_community('clearnet', host=app.config['SERVER_NAME'])
    hidden = make_community('hidden', host='abc.onion')
    clearnet.user_id = hidden.user_id = alice.id
    hidden.site_domain_id = onion.id
    for community in (clearnet, hidden):
        db.session.add(CommunityMember(user_id=alice.id, community_id=community.id, is_owner=True,
                                       is_moderator=True))
    db.session.commit()
    yield SimpleNamespace(default=default, onion=onion, alice=alice, bob=bob, onionite=onionite,
                          clearnet=clearnet, hidden=hidden)
    clear_domain_cache()


def _post(app, community, author, local_only=False, sticky=False):
    post = make_post(community, author, None)
    post.ap_id = f"{app.config['SERVER_URL']}/post/{post.id}"
    post.local_only = local_only
    post.sticky = sticky
    db.session.commit()
    return post


def _reply(app, post, author, local_only=False):
    reply = make_post_reply(post, author)
    reply.ap_id = f"{app.config['SERVER_URL']}/comment/{reply.id}"
    reply.local_only = local_only
    db.session.commit()
    return reply


def _get(app, path, host=None):
    headers = {**AP, 'Host': host} if host else AP
    return app.test_client().get(path, headers=headers)


def _webfinger(app, resource):
    return app.test_client().get(f'/.well-known/webfinger?resource={resource}')


def test_a_local_only_account_is_404_to_webfinger_and_activitypub(app, world):
    for path in ('/u/onionite', '/u/onionite/outbox', '/u/onionite/followers', '/u/onionite/following',
                 '/u/onionite/liked'):
        assert _get(app, path).status_code == 404, path
        assert _get(app, path, host='abc.onion').status_code == 404, path
    assert app.test_client().head('/u/onionite', headers=AP).status_code == 404
    assert _webfinger(app, 'acct:onionite@abc.onion').status_code == 404
    assert _webfinger(app, f"acct:onionite@{app.config['SERVER_NAME']}").status_code == 404
    # hell.cloud's own account is served as before
    assert _get(app, '/u/alice').status_code == 200
    assert _webfinger(app, f"acct:alice@{app.config['SERVER_NAME']}").status_code == 200


def test_a_local_only_community_is_404_too(app, world):
    for path in ('/c/hidden', '/c/hidden/outbox', '/c/hidden/featured', '/c/hidden/moderators',
                 '/c/hidden/followers', '/c/hidden/following'):
        assert _get(app, path).status_code == 404, path
        assert _get(app, path, host='abc.onion').status_code == 404, path
    assert _webfinger(app, 'acct:hidden@abc.onion').status_code == 404
    assert _get(app, '/c/clearnet').status_code == 200


def test_a_local_only_post_and_comment_are_404_as_activitypub(app, world):
    federated = _post(app, world.clearnet, world.alice)
    post = _post(app, world.clearnet, world.alice, local_only=True)
    in_hidden = _post(app, world.hidden, world.alice, local_only=True)
    in_hidden.ap_id = f'http://abc.onion/post/{in_hidden.id}'   # its own host: must not be a 301 there either
    reply = _reply(app, federated, world.alice, local_only=True)
    db.session.commit()
    for path in (f'/post/{post.id}', f'/post/{post.id}/replies', f'/post/{post.id}/context',
                 f'/post/{post.id}/likes', f'/post/{post.id}/dislikes', f'/post/{post.id}/shares',
                 f'/post/{in_hidden.id}', f'/comment/{reply.id}', f'/comment/{reply.id}/likes',
                 f'/comment/{reply.id}/dislikes'):
        assert _get(app, path).status_code == 404, path
    assert app.test_client().head(f'/post/{post.id}', headers=AP).status_code == 404
    assert _get(app, f'/post/{federated.id}').status_code == 200


def test_replies_and_context_of_a_federated_post_omit_local_only_replies(app, world):
    post = _post(app, world.clearnet, world.alice)
    ordinary = _reply(app, post, world.alice)
    hidden = _reply(app, post, world.alice, local_only=True)
    replies = _get(app, f'/post/{post.id}/replies').json
    assert [item['id'] for item in replies['orderedItems']] == [ordinary.ap_id]
    assert replies['totalItems'] == 1
    context = _get(app, f'/post/{post.id}/context').json
    assert context['orderedItems'] == [post.ap_id, ordinary.ap_id]
    assert hidden.ap_id not in context['orderedItems']


def test_outbox_and_featured_omit_local_only_posts(app, world):
    federated = _post(app, world.clearnet, world.alice, sticky=True)
    _post(app, world.clearnet, world.alice, local_only=True, sticky=True)
    ordinary = _reply(app, federated, world.alice)
    _reply(app, federated, world.alice, local_only=True)

    outbox = _get(app, '/c/clearnet/outbox').json
    assert outbox['totalItems'] == 1
    assert [item['object']['object']['id'] for item in outbox['orderedItems']] == [federated.ap_id]
    featured = _get(app, '/c/clearnet/featured').json
    assert featured['totalItems'] == 1
    assert [item['id'] for item in featured['orderedItems']] == [federated.ap_id]
    user_outbox = _get(app, '/u/alice/outbox').json
    assert user_outbox['totalItems'] == 2
    assert {item['object']['id'] for item in user_outbox['orderedItems']} == {federated.ap_id, ordinary.ap_id}


def test_liked_omits_local_only_posts_and_comments(app, world):
    """Task 7 carry: federated_like is set before the outbound guard refuses a vote on local-only content."""
    federated = _post(app, world.clearnet, world.bob)
    hidden_post = _post(app, world.clearnet, world.bob, local_only=True)
    hidden_reply = _reply(app, federated, world.bob, local_only=True)
    for post in (federated, hidden_post):
        db.session.add(PostVote(user_id=world.alice.id, author_id=world.bob.id, post_id=post.id, effect=1,
                                federated_like=True))
    db.session.add(PostReplyVote(user_id=world.alice.id, author_id=world.bob.id, post_reply_id=hidden_reply.id,
                                 effect=1, federated_like=True))
    db.session.commit()
    assert _get(app, '/u/alice/liked').json['totalItems'] == 1


def test_shares_of_a_federated_post_leave_local_only_boosters_out(app, world):
    post = _post(app, world.clearnet, world.alice)
    db.session.add_all([PostBoost(user_id=world.bob.id, post_id=post.id),
                        PostBoost(user_id=world.onionite.id, post_id=post.id)])
    db.session.commit()
    assert _get(app, f'/post/{post.id}/shares').json['totalItems'] == 1


def test_nodeinfo_counts_leave_local_only_rows_out(app, world):
    federated = _post(app, world.clearnet, world.alice)
    _post(app, world.clearnet, world.alice, local_only=True)
    _post(app, world.hidden, world.onionite, local_only=True)
    _reply(app, federated, world.alice)
    _reply(app, federated, world.onionite, local_only=True)
    for path in ('/nodeinfo/2.0', '/nodeinfo/2.1'):
        usage = _get(app, path).json['usage']
        assert usage['users'] == {'total': 2, 'activeHalfyear': 2, 'activeMonth': 2}, path
        assert usage['localPosts'] == 1, path
        assert usage['localComments'] == 1, path
    stats = _get(app, '/api/v1/instance').json['stats']
    assert stats['user_count'] == 2
    assert stats['status_count'] == 2
    counts = _get(app, '/api/v3/site').json['site_view']['counts']
    assert (counts['users'], counts['posts'], counts['comments'], counts['communities']) == (2, 1, 1, 1)
    assert (counts['users_active_day'], counts['users_active_week']) == (2, 2)


def test_a_federating_communitys_moderators_and_counts_leave_local_only_members_out(app, world):
    db.session.add_all([CommunityMember(user_id=world.bob.id, community_id=world.clearnet.id),
                        # a moderator row made before this task refused it
                        CommunityMember(user_id=world.onionite.id, community_id=world.clearnet.id,
                                        is_moderator=True),
                        CommunityMember(user_id=world.onionite.id, community_id=world.hidden.id)])
    # onionite follows alice; bob follows alice; alice follows onionite and joined the hidden community
    db.session.add_all([UserFollower(local_user_id=world.onionite.id, remote_user_id=world.alice.id,
                                     is_accepted=True, is_inward=False),
                        UserFollower(local_user_id=world.bob.id, remote_user_id=world.alice.id,
                                     is_accepted=True, is_inward=False),
                        UserFollower(local_user_id=world.alice.id, remote_user_id=world.onionite.id,
                                     is_accepted=True, is_inward=False)])
    db.session.commit()
    moderators = _get(app, '/c/clearnet/moderators').json
    assert moderators['orderedItems'] == [world.alice.public_url()]
    assert moderators['totalItems'] == 1
    assert _get(app, '/c/clearnet/followers').json['totalItems'] == 2   # alice and bob
    assert _get(app, '/u/alice/followers').json['totalItems'] == 1     # bob
    assert _get(app, '/u/alice/following').json['totalItems'] == 1    # clearnet; not onionite, not hidden


def test_a_local_only_account_cannot_be_made_moderator_of_a_federating_community(app, world, monkeypatch):
    monkeypatch.setattr('app.shared.community.authorise_api_user', lambda *a, **k: world.alice)
    with app.test_request_context():
        with pytest.raises(Exception, match='local_only_moderator'):
            shared_community.add_mod_to_community(world.clearnet.id, world.onionite.id, SRC_API, auth='token')
        # the onion's own community may have it, and a hell.cloud account may moderate hell.cloud's
        shared_community.add_mod_to_community(world.hidden.id, world.onionite.id, SRC_API, auth='token')
        shared_community.add_mod_to_community(world.clearnet.id, world.bob.id, SRC_API, auth='token')
    assert not CommunityMember.query.filter_by(community_id=world.clearnet.id, user_id=world.onionite.id).count()
    assert CommunityMember.query.filter_by(community_id=world.hidden.id, user_id=world.onionite.id,
                                           is_moderator=True).count() == 1

    client = app.test_client()
    login(client, world.alice)
    token = csrf(app, client)
    response = client.post(url(app, 'community.community_add_moderator', community_id=world.clearnet.id,
                               user_id=world.onionite.id), data={'csrf_token': token})
    assert response.status_code == 302
    with client.session_transaction() as session:
        assert any('cannot moderate' in message for _, message in session.get('_flashes', []))
    assert not CommunityMember.query.filter_by(community_id=world.clearnet.id, user_id=world.onionite.id).count()


def test_a_public_feed_leaves_local_only_communities_out(app, world):
    feed = make_local_feed('news', public=True)
    feed.user_id = world.alice.id
    for community in (world.clearnet, world.hidden):
        make_feed_item(feed, community)
    db.session.commit()
    outbox = _get(app, '/f/news/outbox').json
    assert (outbox['totalItems'], outbox['orderedItems']) == (1, [world.clearnet.public_url()])
    following = _get(app, '/f/news/following').json
    assert (following['totalItems'], following['items']) == (1, [world.clearnet.public_url()])


def _snapshot_world(app, world):
    world.alice.public_key = 'ALICE-PUBLIC-KEY'
    world.alice.created = datetime(2026, 1, 2, 3, 4, 5)
    world.alice.title = 'Alice'
    world.clearnet.public_key = 'CLEARNET-PUBLIC-KEY'
    world.clearnet.created_at = datetime(2026, 1, 2, 3, 4, 5)
    world.clearnet.last_active = datetime(2026, 2, 3, 4, 5, 6)
    db.session.commit()


ALICE_ACTOR = (b'{"@context":["https://www.w3.org/ns/activitystreams","https://w3id.org/security/v1"],"acceptPrivateMessages":3'
    b',"attributionDomains":["test.piefed.local"],"discoverable":true,"endpoints":{"sharedInbox":"https://test.piefe'
    b'd.local/inbox"},"followers":"https://test.piefed.local/u/alice/followers","following":"https://test.piefed.loc'
    b'al/u/alice/following","id":"https://test.piefed.local/u/alice","inbox":"https://test.piefed.local/u/alice/inbo'
    b'x","indexable":true,"liked":"https://test.piefed.local/u/alice/liked","manuallyApprovesFollowers":false,"name"'
    b':"Alice","outbox":"https://test.piefed.local/u/alice/outbox","preferredUsername":"alice","publicKey":{"id":"ht'
    b'tps://test.piefed.local/u/alice#main-key","owner":"https://test.piefed.local/u/alice","publicKeyPem":"ALICE-PU'
    b'BLIC-KEY"},"published":"2026-01-02T03:04:05+00:00","type":"Person"}\n')
ALICE_WEBFINGER = (b'{"aliases":["https://test.piefed.local/u/alice"],"links":[{"href":"https://test.piefed.local/u/alice","rel":"h'
    b'ttp://webfinger.net/rel/profile-page","type":"text/html"},{"href":"https://test.piefed.local/u/alice","propert'
    b'ies":{"https://www.w3.org/ns/activitystreams#type":"Person"},"rel":"self","type":"application/activity+json"},'
    b'{"rel":"https://w3id.org/fep/3b86/Create","template":"https://test.piefed.local/share?url={object}"}],"subject'
    b'":"acct:alice@test.piefed.local"}\n')
CLEARNET_ACTOR = (b'{"@context":["https://www.w3.org/ns/activitystreams","https://w3id.org/security/v1"],"attributedTo":"https://t'
    b'est.piefed.local/c/clearnet/moderators","defaultPostType":null,"endpoints":{"sharedInbox":"https://test.piefed'
    b'.local/inbox"},"featured":"https://test.piefed.local/c/clearnet/featured","followers":"https://test.piefed.loc'
    b'al/c/clearnet/followers","following":"https://test.piefed.local/c/clearnet/following","genAI":false,"id":"http'
    b's://test.piefed.local/c/clearnet","inbox":"https://test.piefed.local/c/clearnet/inbox","language":[],"lemmy:ta'
    b'gsForPosts":[],"moderators":"https://test.piefed.local/c/clearnet/moderators","name":"clearnet","newModsWanted'
    b'":false,"outbox":"https://test.piefed.local/c/clearnet/outbox","postUrlType":"friendly","postingRestrictedToMo'
    b'ds":false,"postingWarning":null,"preferredUsername":"clearnet","privateMods":false,"publicKey":{"id":"https://'
    b'test.piefed.local/c/clearnet#main-key","owner":"https://test.piefed.local/c/clearnet","publicKeyPem":"CLEARNET'
    b'-PUBLIC-KEY"},"published":"2026-01-02T03:04:05+00:00","questionAnswer":false,"sensitive":false,"tag":[],"type"'
    b':"Group","updated":"2026-02-03T04:05:06+00:00","url":"https://test.piefed.local/c/clearnet"}\n')
CLEARNET_WEBFINGER = (b'{"aliases":["https://test.piefed.local/c/clearnet"],"links":[{"href":"https://test.piefed.local/c/clearnet","r'
    b'el":"http://webfinger.net/rel/profile-page","type":"text/html"},{"href":"https://test.piefed.local/c/clearnet"'
    b',"properties":{"https://www.w3.org/ns/activitystreams#type":"Group"},"rel":"self","type":"application/activity'
    b'+json"},{"rel":"https://w3id.org/fep/3b86/Follow","template":"{object}/subscribe"}],"subject":"acct:clearnet@t'
    b'est.piefed.local"}\n')


def test_hell_cloud_actor_and_webfinger_are_byte_identical(app, world):
    """A snapshot of the documents taken on the code before this task: an account and a community of hell.cloud
    (the default domain) are served exactly as they were."""
    _snapshot_world(app, world)
    server = app.config['SERVER_NAME']
    assert _get(app, '/u/alice').data == ALICE_ACTOR
    assert _webfinger(app, f'acct:alice@{server}').data == ALICE_WEBFINGER
    assert _get(app, '/c/clearnet').data == CLEARNET_ACTOR
    assert _webfinger(app, f'acct:clearnet@{server}').data == CLEARNET_WEBFINGER
