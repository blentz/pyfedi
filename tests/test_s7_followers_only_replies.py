"""Amendment B, S7.3: a local reply under a followers-only post (or reply) is followers-only too, as Mastodon does.
It goes to the replier's accepted followers and the parent's author, never to Public, never Announced. Its Delete
follows it."""
import copy
import json
from types import SimpleNamespace

import pytest

from app import db
from app.constants import VISIBILITY_FOLLOWERS, VISIBILITY_PUBLIC
from app.models import PostReply
from app.shared.tasks import deletes
from app.shared.tasks.notes import send_reply
from app.visibility import followers_only_reply_instances
from tests.factories import (make_community, make_community_member, make_follow, make_instance,
                             make_instance_block, make_post, make_post_reply, make_site, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


class Sent(list):
    def __call__(self, inbox, body, private_key, key_id, *args, **kwargs):
        self.append(SimpleNamespace(inbox=inbox, body=copy.deepcopy(body), key_id=key_id))

    def inboxes(self):
        return sorted(s.inbox for s in self)


@pytest.fixture
def sent(monkeypatch):
    recorder = Sent()
    monkeypatch.setattr('app.shared.tasks.notes.send_post_request', recorder)
    monkeypatch.setattr('app.shared.tasks.deletes.send_post_request', recorder)
    return recorder


def _peer(domain, software='mastodon'):
    instance = make_instance(domain, software=software)
    instance.inbox = f'https://{domain}/inbox'
    db.session.commit()
    return instance


def _world(post_visibility=VISIBILITY_FOLLOWERS, remote_parent_author=False):
    """alice (local, or dave on peer6) wrote the post; bob (local) replies. bob has an accepted follower on peer1 and
    a pending one on peer2; peer3 only follows the community; carol and carl on peer4 are mentioned."""
    local = make_instance('test.piefed.local', software='piefed')
    make_site()
    replier = make_user(local, 'bob', local=True)     # user 1: make_community hardcodes it as the owner
    community = make_community('microblogs')
    peers = {d: _peer(d, s) for d, s in [('peer1.example', 'mastodon'), ('peer2.example', 'mastodon'),
                                          ('peer3.example', 'lemmy'), ('peer4.example', 'mastodon'),
                                          ('peer6.example', 'mastodon')]}
    author = make_user(peers['peer6.example'], 'dave') if remote_parent_author else make_user(local, 'alice', local=True)
    make_follow(replier, make_user(peers['peer1.example'], 'fan'), is_inward=True)
    make_follow(replier, make_user(peers['peer2.example'], 'maybe'), is_accepted=None, is_inward=True)
    make_community_member(make_user(peers['peer3.example'], 'cm'), community)
    carol = make_user(peers['peer4.example'], 'carol')
    carl = make_user(peers['peer4.example'], 'carl')
    post = make_post(community, author, ap_id='https://test.piefed.local/post/1', title='hello')
    post.visibility = post_visibility
    db.session.commit()
    return SimpleNamespace(community=community, author=author, replier=replier, post=post, peers=peers,
                           carol=carol, carl=carl)


def _reply(w, body='agreed @carol@peer4.example and @carl@peer4.example', visibility=VISIBILITY_FOLLOWERS):
    reply = make_post_reply(w.post, w.replier, body)
    reply.visibility = visibility
    db.session.commit()
    return reply


# --------------------------------------------------------------------------
# PostReply.new: inheritance on the local path
# --------------------------------------------------------------------------


def test_a_local_reply_under_a_followers_only_post_is_followers_only(db_session):
    w = _world()
    reply = PostReply.new(w.replier, w.post, None, 'hi', '<p>hi</p>', True, None, False, False)
    assert reply.visibility == VISIBILITY_FOLLOWERS


def test_a_local_reply_under_a_followers_only_reply_is_followers_only(db_session):
    w = _world(post_visibility=VISIBILITY_PUBLIC)
    parent = _reply(w, body='parent')
    reply = PostReply.new(w.replier, w.post, parent, 'hi', '<p>hi</p>', True, None, False, False)
    assert reply.visibility == VISIBILITY_FOLLOWERS


def test_a_local_reply_under_a_public_post_stays_public(db_session):
    w = _world(post_visibility=VISIBILITY_PUBLIC)
    reply = PostReply.new(w.replier, w.post, None, 'hi', '<p>hi</p>', True, None, False, False)
    assert reply.visibility == VISIBILITY_PUBLIC


def test_a_local_reply_under_a_public_reply_stays_public(db_session):
    w = _world(post_visibility=VISIBILITY_PUBLIC)
    parent = _reply(w, body='parent', visibility=VISIBILITY_PUBLIC)
    reply = PostReply.new(w.replier, w.post, parent, 'hi', '<p>hi</p>', True, None, False, False)
    assert reply.visibility == VISIBILITY_PUBLIC


# --------------------------------------------------------------------------
# send_reply
# --------------------------------------------------------------------------


def test_a_followers_only_reply_goes_to_follower_and_mention_hosts_once_each(db_session, sent):
    """carol and carl share peer4: one delivery, not two."""
    w = _world()
    send_reply(_reply(w).id, None, session=db.session)
    assert sent.inboxes() == ['https://peer1.example/inbox', 'https://peer4.example/inbox']


def test_its_addressing_has_no_public_and_names_the_parent_author(db_session, sent):
    """R-FOCC: cc is the parent author and the mentions, never the follower list."""
    w = _world()
    send_reply(_reply(w).id, None, session=db.session)
    assert sent
    for s in sent:
        assert PUBLIC not in json.dumps(s.body)
        assert s.body['to'] == [w.replier.followers_url()]
        assert s.body['cc'] == [w.author.public_url(), w.carol.public_url(), w.carl.public_url()]
        assert s.body['object']['to'] == s.body['to'] and s.body['object']['cc'] == s.body['cc']
        assert s.body['type'] == 'Create'
        assert s.body['actor'] == w.replier.public_url()
        assert s.key_id == w.replier.public_url() + '#main-key'
        assert 'audience' not in s.body and 'audience' not in s.body['object']


def test_its_edit_is_an_update_to_the_same_audience(db_session, sent):
    w = _world()
    send_reply(_reply(w).id, None, edit=True, session=db.session)
    assert sent.inboxes() == ['https://peer1.example/inbox', 'https://peer4.example/inbox']
    for s in sent:
        assert s.body['type'] == 'Update'
        assert PUBLIC not in json.dumps(s.body)


def test_it_is_never_announced(db_session, sent):
    w = _world()
    send_reply(_reply(w).id, None, session=db.session)
    assert all(s.body['type'] != 'Announce' for s in sent)
    assert 'https://peer3.example/inbox' not in sent.inboxes()


def test_a_remote_parent_authors_host_is_sent_it(db_session, sent):
    w = _world(remote_parent_author=True)
    send_reply(_reply(w, body='agreed').id, None, session=db.session)
    assert sent.inboxes() == ['https://peer1.example/inbox', 'https://peer6.example/inbox']


def test_a_blocked_follower_host_is_not_sent_it(db_session, sent):
    w = _world()
    make_instance_block(w.replier, w.peers['peer1.example'])
    send_reply(_reply(w).id, None, session=db.session)
    assert sent.inboxes() == ['https://peer4.example/inbox']


def test_a_blocked_mention_host_is_not_sent_it(db_session, sent):
    w = _world()
    make_instance_block(w.replier, w.peers['peer4.example'])
    send_reply(_reply(w).id, None, session=db.session)
    assert sent.inboxes() == ['https://peer1.example/inbox']


def test_a_followers_only_reply_in_a_remote_community_skips_the_community_inbox(db_session, sent):
    """A remote community's inbox would relay it to the community's followers."""
    w = _world(remote_parent_author=True)
    remote = make_community('elsewhere', host='peer6.example')
    remote.instance_id = w.peers['peer6.example'].id
    remote.ap_inbox_url = 'https://peer6.example/c/elsewhere/inbox'
    w.post.community_id = remote.id
    reply = _reply(w, body='agreed')
    reply.community_id = remote.id
    db.session.commit()
    send_reply(reply.id, None, session=db.session)
    assert sent.inboxes() == ['https://peer1.example/inbox', 'https://peer6.example/inbox']


def test_a_public_reply_is_still_announced(db_session, sent):
    """Control: the open path is unchanged."""
    w = _world(post_visibility=VISIBILITY_PUBLIC)
    send_reply(_reply(w, body='open', visibility=VISIBILITY_PUBLIC).id, None, session=db.session)
    assert any(s.body['type'] == 'Announce' and s.inbox == 'https://peer3.example/inbox' for s in sent)


# --------------------------------------------------------------------------
# followers_only_reply_instances and the Delete
# --------------------------------------------------------------------------


def test_followers_only_reply_instances_adds_a_remote_parent_author(db_session):
    w = _world(remote_parent_author=True)
    reply = _reply(w, body='x')
    assert sorted(i.domain for i in followers_only_reply_instances(reply)) == ['peer1.example', 'peer6.example']


def test_followers_only_reply_instances_skips_a_local_parent_author(db_session):
    w = _world()
    reply = _reply(w, body='x')
    assert [i.domain for i in followers_only_reply_instances(reply)] == ['peer1.example']


def test_followers_only_reply_instances_lists_a_shared_host_once(db_session):
    w = _world()
    w.post.user_id = make_user(w.peers['peer1.example'], 'neighbour').id
    db.session.commit()
    reply = _reply(w, body='x')
    assert [i.domain for i in followers_only_reply_instances(reply)] == ['peer1.example']


def test_followers_only_reply_instances_uses_the_parent_reply_author(db_session):
    w = _world()
    stranger = make_user(_peer('peer8.example'), 'zed')
    parent = make_post_reply(w.post, stranger, 'parent')
    child = _reply(w, body='child')
    child.parent_id = parent.id
    db.session.commit()
    assert sorted(i.domain for i in followers_only_reply_instances(child)) == ['peer1.example', 'peer8.example']


@pytest.mark.parametrize('task', [deletes.delete_reply, deletes.restore_reply], ids=['delete', 'restore'])
def test_deleting_a_followers_only_reply_reaches_its_audience(db_session, sent, task):
    w = _world(remote_parent_author=True)
    reply = _reply(w, body='x')
    task(None, w.replier.id, reply.id)
    assert sent.inboxes() == ['https://peer1.example/inbox', 'https://peer6.example/inbox']
    for s in sent:
        assert PUBLIC not in json.dumps(s.body)
        assert s.body['to'] == [w.replier.followers_url()]
        assert s.body['cc'] == [w.author.public_url()]


@pytest.mark.parametrize('task', [deletes.delete_reply, deletes.restore_reply], ids=['delete', 'restore'])
def test_a_moderator_removal_of_a_followers_only_reply_is_the_repliers(db_session, sent, task):
    """R-FODELETE: sent as the replier, to the replier's audience and mentions, reason kept local; the moderator's
    own follower host (peer7) never saw the reply."""
    w = _world()
    mod = make_user(w.replier.instance, 'mod', local=True)
    make_follow(mod, make_user(_peer('peer7.example'), 'modfan'), is_inward=True)
    reply = _reply(w)
    task(None, mod.id, reply.id, reason='spam')
    assert sent.inboxes() == ['https://peer1.example/inbox', 'https://peer4.example/inbox']
    for s in sent:
        assert s.key_id == w.replier.public_url() + '#main-key'
        assert s.body['actor'] == w.replier.public_url()
        assert s.body['cc'] == [w.author.public_url(), w.carol.public_url(), w.carl.public_url()]
        assert 'summary' not in json.dumps(s.body)
        assert PUBLIC not in json.dumps(s.body)


def test_deleting_a_remote_authors_followers_only_reply_sends_nothing(db_session, sent):
    """We cannot reach a remote author's followers; the local community's fan-out stays skipped (E9)."""
    w = _world(remote_parent_author=True)
    reply = make_post_reply(w.post, w.author, 'theirs')
    reply.visibility = VISIBILITY_FOLLOWERS
    db.session.commit()
    deletes.delete_reply(None, w.replier.id, reply.id)
    assert sent == []
