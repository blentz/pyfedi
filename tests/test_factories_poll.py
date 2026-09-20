"""tests/test_factories_poll.py"""
from app.models import Poll, PollChoice
from tests.factories import (make_poll, make_poll_choice, make_post, make_user,
                             seed_community_owner, make_community)


def test_make_poll_is_keyed_by_its_post_and_finds_its_choices(app, db_session):
    """`Poll.post_id` is the PRIMARY KEY (app/models.py:3744-3745), not a plain
    FK, which is why the dispatcher looks a poll up with
    `session.query(Poll).get(post.id)` rather than filtering. The factory must
    therefore key on the post it is given, and this test proves that lookup
    shape works rather than merely that a row exists.

    `choice_text` is what the Create/Update arm matches a vote against, so the
    two choices below carry distinct text.
    """
    instance = seed_community_owner('peer.example')
    community = make_community(host='peer.example')
    author = make_user(instance, 'author')
    post = make_post(community, author, 'https://peer.example/post/1')

    poll = make_poll(post, mode='multiple')
    first = make_poll_choice(post, 'yes', sort_order=0)
    second = make_poll_choice(post, 'no', sort_order=1)

    assert db_session.get(Poll, post.id) is poll
    assert poll.mode == 'multiple'          # explicitly passed, not a default
    assert poll.local_only is False

    texts = {c.choice_text for c in db_session.query(PollChoice).filter_by(post_id=post.id)}
    assert texts == {'yes', 'no'}
