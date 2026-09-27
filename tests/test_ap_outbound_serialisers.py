"""What this instance sends peers for a poll, a deleted comment, and a vote.

Sub-project 123 -- `post_to_page`'s Question branch, `comment_model_to_json`'s
deleted arms and `is_vote`, all in `app/activitypub/util.py`.

`post_to_page` builds the document a peer receives for one of our posts, so an
exception in it means the post never federates -- quietly, because the failure is
inside a send task and the post looks perfectly correct locally. Two values it
read were reachable as None (D1337).

The existing tests of this function double it out
(`tests/test_ap_content_objects.py` replaces it to test the ROUTE that serves it),
which is right for them and left the branch itself unasserted.
"""
from datetime import timedelta

import pytest
from flask import g

from app import db
from app.activitypub.util import (comment_model_to_json, is_vote, post_to_page)
from app.constants import (POST_TYPE_ARTICLE, POST_TYPE_EVENT, POST_TYPE_IMAGE,
                          POST_TYPE_LINK, POST_TYPE_POLL)
from app.models import File, Poll, PollChoice, Site, utcnow
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = api_baseline.user2
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           post=post, baseline=api_baseline)


def a_poll(post, mode='single', end_poll='soon', choices=('yes', 'no')):
    poll = Poll(post_id=post.id, mode=mode, local_only=False,
                end_poll=utcnow() + timedelta(days=1) if end_poll == 'soon'
                else end_poll)
    db.session.add(poll)
    for order, text in enumerate(choices, start=1):
        db.session.add(PollChoice(post_id=post.id, choice_text=text,
                                  sort_order=order))
    post.type = POST_TYPE_POLL
    db.session.commit()
    return poll


class TestAPollGoingOut:
    def test_a_single_choice_poll_is_a_question(self, env):
        a_poll(env.post, mode='single')
        page = post_to_page(env.post)
        assert page['type'] == 'Question'
        assert 'name' not in page
        assert [choice['name'] for choice in page['oneOf']] == ['yes', 'no']
        assert 'anyOf' not in page

    def test_a_multiple_choice_poll_uses_anyof(self, env):
        a_poll(env.post, mode='multiple')
        page = post_to_page(env.post)
        assert [choice['name'] for choice in page['anyOf']] == ['yes', 'no']
        assert 'oneOf' not in page

    def test_the_title_moves_into_the_content(self, env):
        """A Question has no `name`, so the title has to survive somewhere."""
        a_poll(env.post)
        page = post_to_page(env.post)
        assert env.post.title in page['content']

    def test_the_end_time_is_sent(self, env):
        poll = a_poll(env.post)
        page = post_to_page(env.post)
        assert page['endTime'].startswith(poll.end_poll.strftime('%Y-%m-%d'))

    def test_each_choice_carries_its_vote_count(self, env):
        a_poll(env.post, choices=('yes',))
        choice = PollChoice.query.filter_by(post_id=env.post.id).one()
        choice.num_votes = 4
        db.session.commit()
        page = post_to_page(env.post)
        assert page['oneOf'][0]['replies']['totalItems'] == 4

    def test_the_choices_keep_their_order(self, env):
        a_poll(env.post, choices=('first', 'second', 'third'))
        page = post_to_page(env.post)
        assert [choice['name'] for choice in page['oneOf']] == \
            ['first', 'second', 'third']


class TestAPollThisInstanceCannotDescribe:
    """D1337. Both of these raised, and the post then never federated."""

    def test_a_poll_with_no_end_time_is_sent_as_an_ordinary_post(self, env):
        """`Poll.end_poll` is nullable, and `app/shared/post.py` sets it only when
        the caller supplied one -- while the API schema marks `mode` and `choices`
        required and `end_poll` not. `ap_datetime(None)` was
        `AttributeError: 'NoneType' object has no attribute 'isoformat'`.

        It goes out as a Page: `Post.new` refuses a Question with no endTime
        (D1330), so sending one asks the other end to drop the poll anyway."""
        a_poll(env.post, end_poll=None)
        page = post_to_page(env.post)
        assert page['type'] == 'Page'
        assert page['name'] == env.post.title
        assert 'oneOf' not in page
        assert 'endTime' not in page

    def test_a_post_typed_as_a_poll_with_no_poll_row(self, env):
        """`AttributeError: 'NoneType' object has no attribute 'mode'`."""
        env.post.type = POST_TYPE_POLL
        db.session.commit()
        page = post_to_page(env.post)
        assert page['type'] == 'Page'
        assert page['name'] == env.post.title

    def test_such_a_post_still_carries_everything_else(self, env):
        """The point of degrading rather than raising: the post is still sent."""
        env.post.type = POST_TYPE_POLL
        db.session.commit()
        page = post_to_page(env.post)
        assert page['id'] == env.post.ap_id
        assert page['name'] == env.post.title
        assert 'published' in page

    def test_who_the_post_is_attributed_to(self, env):
        """`attributedTo` is `post.author.ap_public_url`, the COLUMN, while
        `comment_model_to_json` calls `public_url()`, which falls back to
        `SERVER_URL/u/<name>` when the column is empty.

        For a local user the column is filled by `finalize_user_setup`
        (app/utils.py:2950-2952), which registration runs -- so the two agree in
        practice. They disagree for a user who never went through it: their posts
        would go out with `attributedTo: null` while their comments federate
        normally. Recorded rather than repaired, because the setup function is the
        invariant and this test pins both readings."""
        env.author.ap_public_url = 'https://test.piefed.local/u/user2'
        db.session.commit()
        env.post.type = POST_TYPE_POLL
        db.session.commit()
        assert post_to_page(env.post)['attributedTo'] == \
            'https://test.piefed.local/u/user2'
        reply = make_post_reply(env.post, env.author, body='a comment')
        db.session.commit()
        assert comment_model_to_json(reply)['attributedTo'] == \
            env.author.public_url()


class TestTheOtherPostTypes:
    def test_an_ordinary_post_is_a_page(self, env):
        assert post_to_page(env.post)['type'] == 'Page'

    def test_a_link_post_carries_its_url_as_an_attachment(self, env):
        env.post.type = POST_TYPE_LINK
        env.post.url = 'https://example.test/article'
        db.session.commit()
        page = post_to_page(env.post)
        assert page['attachment'] == [{'href': 'https://example.test/article',
                                       'type': 'Link'}]

    def test_an_image_post_carries_the_image(self, env):
        image = File(source_url='https://example.test/photo.png',
                     file_path='app/static/media/posts/ab/cd/photo.webp',
                     alt_text='a photo')
        db.session.add(image)
        db.session.commit()
        env.post.type = POST_TYPE_IMAGE
        env.post.image_id = image.id
        db.session.commit()
        page = post_to_page(env.post)
        assert page['attachment'][0]['type'] == 'Image'
        assert page['attachment'][0]['url'] == 'https://example.test/photo.png'
        assert page['attachment'][0]['name'] == 'a photo'
        assert page['image']['type'] == 'Image'


class TestACommentGoingOut:
    def test_an_ordinary_comment(self, env):
        reply = make_post_reply(env.post, env.author, body='a comment')
        reply.body_html = '<p>a comment</p>'
        db.session.commit()
        data = comment_model_to_json(reply)
        assert data['content'] == '<p>a comment</p>'
        assert data['source']['content'] == 'a comment'

    def test_an_edited_comment_says_when(self, env):
        reply = make_post_reply(env.post, env.author, body='a comment')
        reply.edited_at = utcnow()
        db.session.commit()
        assert 'updated' in comment_model_to_json(reply)

    def test_one_the_author_deleted(self, env):
        reply = make_post_reply(env.post, env.author, body='a comment')
        reply.body_html = '<p>a comment</p>'
        reply.deleted = True
        reply.deleted_by = reply.user_id
        db.session.commit()
        data = comment_model_to_json(reply)
        assert data['content'] == '<p>Deleted by author</p>'
        assert data['source']['content'] == 'Deleted by author'
        assert 'a comment' not in data['content']

    def test_one_a_moderator_deleted(self, env):
        """Who removed it is what a reader of the other instance is told."""
        moderator = make_user(env.baseline.instance_local, 'amod', local=True)
        db.session.commit()
        reply = make_post_reply(env.post, env.author, body='a comment')
        reply.body_html = '<p>a comment</p>'
        reply.deleted = True
        reply.deleted_by = moderator.id
        db.session.commit()
        data = comment_model_to_json(reply)
        assert data['content'] == '<p>Deleted by moderator</p>'
        assert data['source']['content'] == 'Deleted by moderator'

    def test_a_deleted_comment_with_no_deleter_recorded(self, env):
        """`deleted_by` is nullable, and None is not the author's id, so this
        reads as a moderator's removal."""
        reply = make_post_reply(env.post, env.author, body='a comment')
        reply.body_html = '<p>a comment</p>'
        reply.deleted = True
        reply.deleted_by = None
        db.session.commit()
        assert comment_model_to_json(reply)['content'] == \
            '<p>Deleted by moderator</p>'


class TestRecognisingAVote:
    @pytest.mark.parametrize('activity,expected', [
        ({'type': 'Like'}, True),
        ({'type': 'Dislike'}, True),
        ({'type': 'Announce', 'object': {'type': 'Like'}}, True),
        ({'type': 'Announce', 'object': {'type': 'Dislike'}}, True),
        ({'type': 'Announce', 'object': {'type': 'Create'}}, False),
        ({'type': 'Announce', 'object': 'https://peer.test/like/1'}, False),
        ({'type': 'Announce', 'object': {}}, False),
        ({'type': 'Announce'}, False),
        ({'type': 'Create'}, False),
        ({'type': 'Undo', 'object': {'type': 'Like'}}, False),
        ({}, False),
        ({'type': None}, False),
        ({'type': 5}, False),
    ])
    def test_what_counts_as_a_vote(self, activity, expected):
        assert is_vote(activity) is expected

    def test_it_never_raises(self):
        """Its `except Exception: return False` is what makes the unguarded
        `activity['object']['type']` above it safe, so the property is that
        nothing gets out."""
        for activity in (None, [], 'a string', 5, {'type': 'Announce', 'object': 5},
                         {'type': 'Announce', 'object': []}):
            assert is_vote(activity) is False


class TestAnEventGoingOut:
    """D1338, the same two reachable Nones as the poll branch one above it."""

    def an_event(self, post, start='soon', end='later', timezone='Europe/London'):
        from app.models import Event

        event = Event(post_id=post.id, timezone=timezone,
                      start=utcnow() + timedelta(days=1) if start == 'soon' else start,
                      end=utcnow() + timedelta(days=2) if end == 'later' else end)
        db.session.add(event)
        post.type = POST_TYPE_EVENT
        db.session.commit()
        return event

    def test_an_event_with_both_times(self, env):
        event = self.an_event(env.post)
        page = post_to_page(env.post)
        assert page['type'] == 'Event'
        assert page['startTime'].startswith(event.start.strftime('%Y-%m-%d'))
        assert page['endTime'].startswith(event.end.strftime('%Y-%m-%d'))
        assert page['timezone'] == 'Europe/London'

    def test_an_event_with_no_end_time(self, env):
        """An end time is optional in the document, and `Post.new` reads it with
        `.get` (D1339), so a peer running PieFed takes the event without one."""
        self.an_event(env.post, end=None)
        page = post_to_page(env.post)
        assert page['type'] == 'Event'
        assert 'startTime' in page
        assert 'endTime' not in page

    def test_an_event_with_no_start_time_is_sent_as_an_ordinary_post(self, env):
        """`ap_datetime(None)` was `AttributeError: 'NoneType' object has no
        attribute 'isoformat'`, and `app/shared/post.py` sets `start` only when
        the caller supplied one."""
        self.an_event(env.post, start=None)
        page = post_to_page(env.post)
        assert page['type'] == 'Page'
        assert page['name'] == env.post.title
        assert 'startTime' not in page
        assert 'endTime' not in page

    def test_a_post_typed_as_an_event_with_no_event_row(self, env):
        """`AttributeError: 'NoneType' object has no attribute 'start'`."""
        env.post.type = POST_TYPE_EVENT
        db.session.commit()
        page = post_to_page(env.post)
        assert page['type'] == 'Page'
        assert page['name'] == env.post.title

    def test_such_a_post_is_still_sent(self, env):
        env.post.type = POST_TYPE_EVENT
        db.session.commit()
        page = post_to_page(env.post)
        assert page['id'] == env.post.ap_id
        assert 'published' in page

    def test_an_event_with_no_timezone(self, env):
        """`Event.timezone` is nullable and nothing here requires it."""
        self.an_event(env.post, timezone=None)
        page = post_to_page(env.post)
        assert page['type'] == 'Event'
        assert page['timezone'] is None
