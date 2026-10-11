"""Amendment B, S7.2: a followers-only post in local microblogs reaches the author's accepted followers and the
people it mentions -- nobody else. No community Announce, no Public anywhere, canQuote limited to the author.

Deliveries are captured by replacing each module's `send_post_request` (the precedent is tests/test_chat_util.py and
tests/test_visibility_outbound.py). No signature is involved, so nothing honest is lost.
"""
import copy
import json
from types import SimpleNamespace

import pytest

from app import db
from app.constants import POST_TYPE_ARTICLE, VISIBILITY_FOLLOWERS
from app.models import UserFollower
from app.shared.tasks import deletes
from app.shared.tasks.pages import send_post
from app.visibility import follower_instances
from tests.factories import (make_community, make_community_member, make_follow, make_instance, make_post,
                             make_site, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


class Sent(list):
    def __call__(self, inbox, body, private_key, key_id, *args, **kwargs):
        self.append(SimpleNamespace(inbox=inbox, body=copy.deepcopy(body), key_id=key_id))

    def inboxes(self):
        return sorted(s.inbox for s in self)


@pytest.fixture
def sent(monkeypatch):
    recorder = Sent()
    monkeypatch.setattr('app.shared.tasks.pages.send_post_request', recorder)
    monkeypatch.setattr('app.shared.tasks.deletes.send_post_request', recorder)
    return recorder


def _peer(domain, software='mastodon'):
    instance = make_instance(domain, software=software)
    instance.inbox = f'https://{domain}/inbox'
    db.session.commit()
    return instance


def _world(visibility=VISIBILITY_FOLLOWERS):
    """Instance 1 is local (built first, fact 21). Five peers:

    peer1 (mastodon) and peer5 (piefed) each host an ACCEPTED follower of alice; peer2 hosts a PENDING one;
    peer3 (lemmy) only follows the community; peer4 hosts carol, who is mentioned."""
    local = make_instance('test.piefed.local', software='piefed')
    make_site()
    author = make_user(local, 'alice', local=True)
    community = make_community('microblogs')
    peers = {d: _peer(d, s) for d, s in [('peer1.example', 'mastodon'), ('peer2.example', 'mastodon'),
                                          ('peer3.example', 'lemmy'), ('peer4.example', 'mastodon'),
                                          ('peer5.example', 'piefed')]}
    make_follow(author, make_user(peers['peer1.example'], 'bob'), is_inward=True)
    make_follow(author, make_user(peers['peer5.example'], 'eve'), is_inward=True)
    make_follow(author, make_user(peers['peer2.example'], 'pat'), is_accepted=None, is_inward=True)
    make_community_member(make_user(peers['peer3.example'], 'cm'), community)
    make_user(peers['peer4.example'], 'carol')
    post = make_post(community, author, ap_id='https://test.piefed.local/post/1', title='hello')
    post.type = POST_TYPE_ARTICLE
    post.body = 'for friends, and @carol@peer4.example'
    post.body_html = '<p>for friends</p>'
    post.visibility = visibility
    db.session.commit()
    return SimpleNamespace(author=author, community=community, post=post, peers=peers)


# --------------------------------------------------------------------------
# follower_instances
# --------------------------------------------------------------------------


def test_follower_instances_counts_accepted_inward_followers_only(db_session):
    w = _world()
    outward = _peer('peer6.example')
    make_follow(w.author, make_user(outward, 'followed'))          # alice follows them: not a follower
    dormant = _peer('peer7.example')
    dormant.dormant = True
    make_follow(w.author, make_user(dormant, 'sleepy'), is_inward=True)
    make_follow(w.author, make_user(w.peers['peer1.example'], 'bob2'), is_inward=True)   # second follower, same host
    db.session.commit()
    assert sorted(i.domain for i in follower_instances(w.author)) == ['peer1.example', 'peer5.example']


# --------------------------------------------------------------------------
# send_post
# --------------------------------------------------------------------------


def test_a_followers_only_post_goes_to_follower_hosts_and_mentions_only(db_session, sent):
    w = _world()
    send_post(w.post.id, session=db.session)
    assert sent.inboxes() == ['https://peer1.example/inbox', 'https://peer4.example/inbox',
                              'https://peer5.example/inbox']


@pytest.mark.parametrize('edit', [False, True], ids=['create', 'update'])
def test_no_public_anywhere_and_addressed_to_the_followers_collection(db_session, sent, edit):
    w = _world()
    send_post(w.post.id, edit=edit, session=db.session)
    assert sent
    for s in sent:
        assert PUBLIC not in json.dumps(s.body)
        assert s.body['type'] == ('Update' if edit else 'Create')
        assert s.body['to'] == [w.author.followers_url()]
        assert s.body['object']['to'] == [w.author.followers_url()]
        assert s.body['object']['type'] == 'Note'
        assert 'audience' not in s.body and 'audience' not in s.body['object']


def test_mentions_are_in_cc(db_session, sent):
    w = _world()
    send_post(w.post.id, session=db.session)
    carol = 'https://peer4.example/users/carol'
    assert all(carol in s.body['cc'] for s in sent)


def test_no_announce_and_never_signed_by_the_community(db_session, sent):
    w = _world()
    send_post(w.post.id, session=db.session)
    assert all(s.body['type'] != 'Announce' for s in sent)
    assert all(s.key_id == w.author.public_url() + '#main-key' for s in sent)


def test_can_quote_is_the_author_only(db_session, sent):
    w = _world()
    send_post(w.post.id, session=db.session)
    for s in sent:
        assert s.body['object']['interactionPolicy']['canQuote']['automaticApproval'] == [w.author.public_url()]


def test_follower_cc_names_accepted_followers_only(db_session, sent):
    w = _world()
    send_post(w.post.id, session=db.session)
    last = sent[-1].body['cc']
    assert 'https://peer1.example/users/bob' in last
    assert 'https://peer2.example/users/pat' not in last


def test_with_no_accepted_followers_only_mentions_are_sent(db_session, sent):
    w = _world()
    UserFollower.query.filter_by(local_user_id=w.author.id, is_accepted=True).delete()
    db.session.commit()
    send_post(w.post.id, session=db.session)
    assert sent.inboxes() == ['https://peer4.example/inbox']


def test_a_public_post_in_microblogs_is_still_announced(db_session, sent):
    """Control: the public path is unchanged -- the community's follower host gets the Announce."""
    w = _world(visibility='public')
    w.author.private_key = 'k'
    db.session.commit()
    send_post(w.post.id, session=db.session)
    assert any(s.inbox == 'https://peer3.example/inbox' and s.body['type'] == 'Announce' for s in sent)


# --------------------------------------------------------------------------
# delete_object: the Delete follows the post, with no Public
# --------------------------------------------------------------------------


def test_deleting_a_followers_only_post_tells_follower_hosts_without_public(db_session, sent):
    w = _world()
    deletes.delete_post(None, w.author.id, w.post.id)
    assert sent.inboxes() == ['https://peer1.example/inbox', 'https://peer5.example/inbox']
    for s in sent:
        assert PUBLIC not in json.dumps(s.body)
        assert s.body['to'] == [w.author.followers_url()]
        assert 'https://peer2.example/users/pat' not in s.body['cc']
        assert 'audience' not in s.body


def test_deleting_a_public_post_keeps_public_addressing(db_session, sent):
    w = _world(visibility='public')
    deletes.delete_post(None, w.author.id, w.post.id)
    assert sent
    assert all(s.body['to'] == [PUBLIC] or s.body['object']['to'] == [PUBLIC] for s in sent)
