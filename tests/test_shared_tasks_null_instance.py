"""D331, fixed: a community with no instance_id federates nothing.

`Community.instance_id` is a nullable FK. The senders in app/shared/tasks/
read `community.instance.online()` with no `instance_id` check, so such a row
raised AttributeError on NoneType mid-task. Each site now checks the FK first,
as app/activitypub/util.py's refresh tasks do, and treats a community with no
instance as one it cannot deliver for.
"""
import pytest

from app import db
from app.constants import SRC_API
from app.models import ActivityPubLog
from app.shared.tasks.adds import add_object
from app.shared.tasks.blocks import ban_person
from app.shared.tasks.deletes import delete_object
from app.shared.tasks.flags import report_object
from app.shared.tasks.follows import join_community, leave_community
from app.shared.tasks.groups import edit_community
from app.shared.tasks.likes import send_vote, vote_for_poll
from app.shared.tasks.locks import lock_object
from app.shared.tasks.notes import send_answer, send_reply
from app.shared.tasks.pages import move_object, send_post
from app.shared.tasks.removes import remove_object
from tests.factories import (
    make_community, make_community_member, make_instance, make_poll, make_poll_choice, make_post,
    make_post_reply, make_user,
)


@pytest.fixture
def scene(db_session):
    local = make_instance('test.piefed.local', software='piefed')
    user = make_user(local, 'mod', local=True, with_keys=True)
    community = make_community('orphan', host='peer.example')
    post = make_post(community, user, None)
    reply = make_post_reply(post, user)
    make_community_member(user, community)
    make_poll(post)
    make_poll_choice(post, 'a')
    community.ap_id = 'orphan@peer.example'  # remote, so the join/leave arms are reached
    community.instance_id = None
    db.session.commit()
    return user, community, post, reply


CALLS = {
    'report_object': lambda u, c, p, r: report_object(db.session, u.id, p, 'spam', [1]),
    'lock_object': lambda u, c, p, r: lock_object(db.session, u.id, p),
    'remove_object': lambda u, c, p, r: remove_object(db.session, u.id, p),
    'add_object': lambda u, c, p, r: add_object(db.session, u.id, p),
    'move_object': lambda u, c, p, r: move_object(db.session, u.id, p, c, c),
    'ban_person': lambda u, c, p, r: ban_person(db.session, u.id, u.id, c.id, None, 'r', False),
    'send_reply': lambda u, c, p, r: send_reply(r.id, None, session=db.session),
    'send_answer': lambda u, c, p, r: send_answer(r.id, u.id, False),
    'delete_object': lambda u, c, p, r: delete_object(u.id, p, is_post=True, session=db.session),
    'send_vote': lambda u, c, p, r: send_vote(u.id, p, None, 'upvote', None),
    'vote_for_poll': lambda u, c, p, r: vote_for_poll(None, u.id, p.id, 'a'),
    'send_post': lambda u, c, p, r: send_post(p.id, session=db.session),
    'edit_community': lambda u, c, p, r: edit_community(None, u.id, c.id),
    'join_community': lambda u, c, p, r: join_community(None, u.id, c.id, SRC_API),
    'leave_community': lambda u, c, p, r: leave_community(None, u.id, c.id),
}


@pytest.mark.parametrize('name', sorted(CALLS))
def test_a_community_with_no_instance_federates_nothing(scene, name):
    user, community, post, reply = scene

    CALLS[name](user, community, post, reply)

    assert db.session.query(ActivityPubLog).filter_by(direction='out').count() == 0
