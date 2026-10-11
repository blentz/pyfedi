"""Phase S6 of the security track: a signed GET by a requester entitled to see it unlocks a local author's
followers-only post or comment. Spec: "Unlocking followers-only content".

The followers-only post and reply are authored through S7.1 and S7.3. Entitled = an accepted follower of the author,
or any signer on a host that holds one. What an entitled signer gets is addressed exactly as the push was.
"""
import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.activitypub.signature import HttpSignature
from app.constants import POST_TYPE_ARTICLE, SRC_API
from app.models import Language, PostReply
from app.shared.post import make_post as shared_make_post
from app.shared.tasks.notes import send_reply
from app.shared.tasks.pages import send_post
from tests.factories import (make_community, make_follow, make_instance, make_instance_block, make_post,
                             make_post_reply, make_site, make_user)
from tests.test_ap_content_objects import AP_ACCEPT, ap_get, signed_ap_get
from tests.test_shared_post_edit import _api_input

UA = 'Test (+https://peer2.example)'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def signed_ap_head(app, path, signer):
    """signed_ap_get's HEAD: the signature covers the method, so it is made for 'head'. signed_request adds an
    Accept only to a GET, so the ActivityPub one is set here, unsigned (GET_REQUIRED does not need it)."""
    with patch('app.activitypub.signature.is_invalid_get_request_uri', return_value=False):
        _, headers, _ = HttpSignature.signed_request(
            f'https://test.piefed.local{path}', None, signer.private_key, f'{signer.ap_profile_id}#main-key',
            method='head', send_via_async=True)
    headers['User-Agent'] = UA
    headers['Accept'] = AP_ACCEPT
    with app.test_client() as client:
        return client.head(path, headers=headers)


def _vary(response):
    return [part.strip() for part in response.headers['Vary'].split(',')]


def _peer(domain):
    instance = make_instance(domain)
    instance.inbox = f'https://{domain}/inbox'
    db.session.commit()
    return instance


@pytest.fixture
def world(app, db_session, monkeypatch):
    local = make_instance('test.piefed.local', software='piefed')
    site = make_site()
    site.private_instance = False
    g.admin_ids = []
    g.site = site
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    author = make_user(local, 'alice', local=True, with_keys=True)    # user 1: make_community's owner
    community = make_community('microblogs')
    with monkeypatch.context() as m:
        m.setattr('app.shared.post.task_selector', lambda *a, **k: None)
        m.setattr('app.shared.post.authorise_api_user', lambda *a, **k: author)
        _, post = shared_make_post(_api_input(visibility='followers', title='for friends'), community,
                                   POST_TYPE_ARTICLE, SRC_API, auth='t')
    reply = PostReply.new(author, post, None, 'reply', '<p>reply</p>', True, None, False, False)
    assert reply.visibility == 'followers'
    follower_host = _peer('peer2.example')
    follower = make_user(follower_host, 'bob', with_keys=True)
    neighbour = make_user(follower_host, 'dave', with_keys=True)
    stranger = make_user(_peer('peer3.example'), 'eve', with_keys=True)
    make_follow(author, follower, is_inward=True)    # bob follows the local author, accepted
    return SimpleNamespace(community=community, author=author, post=post, reply=reply, follower=follower,
                           neighbour=neighbour, stranger=stranger)


def _assert_private(response):
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    assert 'Signature' in _vary(response)


def test_an_accepted_follower_gets_a_followers_only_post_privately(app, world):
    response = signed_ap_get(app, f'/post/{world.post.id}', world.follower, UA)

    _assert_private(response)
    # post_to_page always says Public; what an entitled signer gets says who it is for
    assert response.json['to'] == [world.author.followers_url()]
    assert response.json['cc'] == []
    assert response.json['interactionPolicy'] == {'canQuote': {'automaticApproval': [world.author.public_url()]}}
    assert 'audience' not in response.json
    assert PUBLIC not in response.get_data(as_text=True)


def test_a_signer_on_a_followers_host_gets_it_too(app, world):
    _assert_private(signed_ap_get(app, f'/post/{world.post.id}', world.neighbour, UA))


def test_a_signer_sharing_no_host_with_a_follower_gets_404(app, world):
    assert signed_ap_get(app, f'/post/{world.post.id}', world.stranger, UA).status_code == 404


def test_an_unsigned_request_gets_404(app, world):
    assert ap_get(app, f'/post/{world.post.id}').status_code == 404


def test_a_pending_follow_does_not_unlock_it(app, world):
    pending = make_user(make_instance('peer4.example'), 'pat', with_keys=True)
    make_follow(world.author, pending, is_accepted=None, is_inward=True)

    assert signed_ap_get(app, f'/post/{world.post.id}', pending, UA).status_code == 404


def test_an_entitled_answer_is_not_left_for_the_next_requester(app, world):
    assert signed_ap_get(app, f'/post/{world.post.id}', world.follower, UA).status_code == 200
    assert ap_get(app, f'/post/{world.post.id}').status_code == 404


def test_a_followers_only_comment_is_served_privately_to_a_follower(app, world):
    response = signed_ap_get(app, f'/comment/{world.reply.id}', world.follower, UA)

    _assert_private(response)
    assert response.json['to'] == [world.author.followers_url()]
    # as send_reply cc's it (R-FOCC): the parent's author, then any mentions -- here alice answers herself
    assert response.json['cc'] == [world.author.public_url()]
    assert response.json['interactionPolicy'] == {'canQuote': {'automaticApproval': [world.author.public_url()]}}
    assert 'audience' not in response.json
    assert PUBLIC not in response.get_data(as_text=True)


def test_a_followers_only_comment_is_404_unsigned(app, world):
    assert ap_get(app, f'/comment/{world.reply.id}').status_code == 404


def test_a_followers_only_comment_is_404_to_a_stranger(app, world):
    assert signed_ap_get(app, f'/comment/{world.reply.id}', world.stranger, UA).status_code == 404


@pytest.mark.parametrize('path', ['/post/{post}', '/comment/{reply}'])
def test_a_signed_head_is_answered_privately_with_no_body(app, world, path):
    response = signed_ap_head(app, path.format(post=world.post.id, reply=world.reply.id), world.follower)

    _assert_private(response)
    assert response.get_data() == b''


@pytest.mark.parametrize('suffix', ['/replies', '/context'])
def test_a_followers_only_posts_collections_are_served_privately_to_a_follower(app, world, suffix):
    _assert_private(signed_ap_get(app, f'/post/{world.post.id}{suffix}', world.follower, UA))


def test_a_remote_authors_followers_only_post_stays_404_even_for_a_follower(app, world):
    remote_post = make_post(world.community, world.stranger, 'https://peer3.example/p/1')
    remote_post.visibility = 'followers'
    db.session.commit()
    make_follow(world.stranger, world.follower, is_inward=True)

    assert signed_ap_get(app, f'/post/{remote_post.id}/replies', world.follower, UA).status_code == 404


def test_a_direct_post_stays_404_for_a_follower(app, world):
    world.post.visibility = 'direct'
    db.session.commit()

    assert signed_ap_get(app, f'/post/{world.post.id}', world.follower, UA).status_code == 404


def test_a_public_post_keeps_its_public_cache_headers(app, world):
    world.post.visibility = 'public'
    db.session.commit()

    response = ap_get(app, f'/post/{world.post.id}')

    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'public, max-age=120'


@pytest.mark.parametrize('suffix', ['/replies', '/context'])
@pytest.mark.parametrize('who', ['stranger', None])
def test_a_followers_only_posts_collections_are_404_to_anyone_else(app, world, suffix, who):
    path = f'/post/{world.post.id}{suffix}'
    response = signed_ap_get(app, path, world.stranger, UA) if who else ap_get(app, path)
    assert response.status_code == 404


def test_an_author_who_blocks_a_followers_instance_still_refuses_it(app, world):
    """D194 holds on the unlock path: entitlement does not outrank the author's instance block."""
    make_instance_block(world.author, world.follower.instance)

    assert signed_ap_get(app, f'/post/{world.post.id}', world.follower, UA).status_code == 401
    assert signed_ap_get(app, f'/comment/{world.reply.id}', world.follower, UA).status_code == 401


# --------------------------------------------------------------------------
# R-FETCHEQPUSH: whoever was pushed it may fetch it
# --------------------------------------------------------------------------


@pytest.fixture
def mentioned(world):
    """carol on peer4 is Mentioned in the post, so send_post cc'd her; carl shares her host. Neither follows alice."""
    host = _peer('peer4.example')
    carol = make_user(host, 'carol', with_keys=True)
    carl = make_user(host, 'carl', with_keys=True)
    world.post.body = 'for friends, and @carol@peer4.example'
    db.session.commit()
    return SimpleNamespace(carol=carol, carl=carl)


def test_a_mentioned_user_gets_the_post(app, world, mentioned):
    response = signed_ap_get(app, f'/post/{world.post.id}', mentioned.carol, UA)

    _assert_private(response)
    assert response.json['cc'] == [mentioned.carol.public_url()]


def test_a_signer_on_a_mentioned_users_host_gets_the_post(app, world, mentioned):
    _assert_private(signed_ap_get(app, f'/post/{world.post.id}', mentioned.carl, UA))


def test_a_mention_does_not_unlock_it_for_a_stranger(app, world, mentioned):
    assert signed_ap_get(app, f'/post/{world.post.id}', world.stranger, UA).status_code == 404


def test_a_remote_parent_author_who_does_not_follow_gets_the_comment(app, world):
    rob = make_user(_peer('peer5.example'), 'rob', with_keys=True)
    parent = make_post_reply(world.post, rob)
    reply = PostReply.new(world.author, world.post, parent, 'yes', '<p>yes</p>', True, None, False, False)
    assert reply.visibility == 'followers'

    response = signed_ap_get(app, f'/comment/{reply.id}', rob, UA)

    _assert_private(response)
    assert response.json['cc'] == [rob.public_url()]
    assert signed_ap_get(app, f'/comment/{reply.id}', world.stranger, UA).status_code == 404


# --------------------------------------------------------------------------
# a deleted followers-only object: its Tombstone is private too
# --------------------------------------------------------------------------


@pytest.mark.parametrize('kind', ['post', 'comment'])
def test_a_deleted_followers_only_objects_tombstone_is_private(app, world, kind):
    obj = world.post if kind == 'post' else world.reply
    obj.deleted = True
    db.session.commit()
    path = f'/{kind}/{obj.id}'

    response = signed_ap_get(app, path, world.follower, UA)

    assert response.status_code == 410
    assert response.json['type'] == 'Tombstone'
    # ap_miss_is_not_cached rewrites every 404 and 410 to plain no-store, which is stricter than private, no-store
    assert response.headers['Cache-Control'] == 'no-store'
    assert 'Signature' in _vary(response)
    assert signed_ap_get(app, path, world.stranger, UA).status_code == 404
    assert ap_get(app, path).status_code == 404


def test_a_deleted_public_posts_tombstone_does_not_vary_on_the_signer(app, world):
    world.post.visibility = 'public'
    world.post.deleted = True
    db.session.commit()

    response = ap_get(app, f'/post/{world.post.id}')

    assert response.status_code == 410
    assert 'Signature' not in _vary(response)


# --------------------------------------------------------------------------
# what is fetched is addressed as what was pushed (Task 27 review carry-over)
# --------------------------------------------------------------------------


class Sent(list):
    def __call__(self, inbox, body, private_key, key_id, *args, **kwargs):
        self.append(copy.deepcopy(body))


def _addressing(document):
    return {key: document.get(key) for key in ('to', 'cc', 'audience', 'interactionPolicy')}


def test_a_fetched_post_is_addressed_as_its_pushed_create(app, world, monkeypatch):
    world.post.body = 'for friends, and @eve@peer3.example'
    db.session.commit()
    sent = Sent()
    monkeypatch.setattr('app.shared.tasks.pages.send_post_request', sent)
    send_post(world.post.id, session=db.session)
    assert sent and all(body['type'] == 'Create' for body in sent)

    fetched = signed_ap_get(app, f'/post/{world.post.id}', world.follower, UA).json

    assert fetched['cc'] == [world.stranger.public_url()]
    for body in sent:
        assert _addressing(fetched) == _addressing(body['object'])
        assert (body['to'], body['cc']) == (fetched['to'], fetched['cc'])


def test_a_fetched_comment_is_addressed_as_its_pushed_create(app, world, monkeypatch):
    reply = PostReply.new(world.author, world.post, world.reply, 'hi @eve@peer3.example',
                          '<p>hi @eve@peer3.example</p>', True, None, False, False)
    sent = Sent()
    monkeypatch.setattr('app.shared.tasks.notes.send_post_request', sent)
    send_reply(reply.id, world.reply.id, session=db.session)
    assert sent and all(body['type'] == 'Create' for body in sent)

    fetched = signed_ap_get(app, f'/comment/{reply.id}', world.follower, UA).json

    assert fetched['cc'] == [world.author.public_url(), world.stranger.public_url()]
    for body in sent:
        assert _addressing(fetched) == _addressing(body['object'])
        assert (body['to'], body['cc']) == (fetched['to'], fetched['cc'])
