"""`Post.new`'s last three branches: a mention, a poll, and the AI check.

Sub-project 119, continuing 103's work on the same method. Everything here
arrives in a Create from a peer, and each of these reads used to lose the whole
post rather than the part it could not understand:

* the Mention branch called `db.session.get(User, post.user_id).first()`, and
  `db.session.get` answers a model or None and has no `.first()` -- so EVERY
  federated post mentioning a local user who had not blocked the sender was
  `AttributeError: 'User' object has no attribute 'first'` (D1329);
* the Question branch read `endTime`, `oneOf` and each choice's `name` outright:
  a KeyError apiece, a TypeError for a choice that is a string, and a DataError
  for an endTime that is not a date, which poisons the transaction too (D1330);
* the AI check read `confidence` and `detection_result` off whatever the
  configured endpoint returned (D1331).
"""
import httpx
import pytest
from flask import current_app, g

from app import db
from app.constants import POST_TYPE_ARTICLE, POST_TYPE_POLL
from app.models import (Notification, Poll, PollChoice, Post, Site, User,
                        UserBlock, parse_poll_end_time)
from tests.factories import (make_community, make_community_member, make_user)

AUTHOR = 'https://remote.test/u/someone'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@pytest.fixture
def env(app, api_baseline, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = make_user(api_baseline.instance_remote, 'someone')
    author.ap_id = 'someone@remote.test'
    author.ap_profile_id = AUTHOR
    author.ap_public_url = AUTHOR
    db.session.commit()
    make_community_member(author, community)
    monkeypatch.setitem(current_app.config, 'IMAGE_HASHING_ENDPOINT', '')
    local = make_user(api_baseline.instance_local, 'localuser', local=True)
    local.ap_profile_id = f"{current_app.config['SERVER_URL']}/u/localuser"
    db.session.commit()
    return SimpleNamespace(community=community, author=author, local=local,
                           baseline=api_baseline)


def an_activity(**overrides):
    obj = {'id': f'https://remote.test/p/{abs(hash(str(sorted(overrides)))) % 100000}',
           'type': 'Page', 'name': 'a post', 'attributedTo': AUTHOR,
           'to': [PUBLIC], 'published': '2026-01-01T00:00:00Z'}
    obj.update(overrides)
    return {'id': 'https://remote.test/c/1', 'type': 'Create', 'to': [PUBLIC],
            'object': obj}


def new(env, **overrides):
    return Post.new(env.author, env.community, an_activity(**overrides))


class TestAPostThatMentionsSomebodyHere:
    """D1329. The whole branch had never run once."""

    def a_mention(self, href, **extra):
        tag = {'type': 'Mention', 'href': href}
        tag.update(extra)
        return [tag]

    def test_the_post_arrives(self, env):
        post = new(env, tag=self.a_mention(env.local.ap_profile_id))
        assert post is not None
        assert post.title == 'a post'

    def test_the_mentioned_user_is_notified(self, env):
        post = new(env, tag=self.a_mention(env.local.ap_profile_id))
        notification = Notification.query.filter_by(user_id=env.local.id).one()
        assert str(post.id) in notification.url
        assert notification.author_id == env.author.id

    def test_their_unread_count_goes_up(self, env):
        before = env.local.unread_notifications
        new(env, tag=self.a_mention(env.local.ap_profile_id))
        db.session.refresh(env.local)
        assert env.local.unread_notifications == before + 1

    def test_the_notification_names_the_author(self, env):
        new(env, tag=self.a_mention(env.local.ap_profile_id))
        notification = Notification.query.filter_by(user_id=env.local.id).one()
        assert notification.targets['author_user_name'] == 'someone@remote.test'

    def test_a_mention_whose_path_is_in_another_case_still_finds_them(self, env):
        """The href is lowercased before the lookup, because `ap_profile_id` is
        stored lowercase. Only the PATH can differ: the `startswith(SERVER_URL)`
        gate runs BEFORE the lowercasing, so an href whose scheme or host is
        uppercased is dropped instead -- correct for a URL comparison it is not,
        but no peer sends one, and the test below says so rather than leaving the
        asymmetry undocumented."""
        profile = env.local.ap_profile_id
        server = current_app.config['SERVER_URL']
        post = new(env, tag=self.a_mention(
            server + profile[len(server):].upper()))
        assert post is not None
        assert Notification.query.filter_by(user_id=env.local.id).count() == 1

    def test_a_mention_whose_host_is_uppercased_is_not_matched(self, env):
        """Recorded, not repaired: `profile_id.startswith(SERVER_URL)` is
        case-sensitive, and a URL's scheme and host are not. Every implementation
        that sends a mention copies the profile url verbatim, so this costs
        nothing today -- but it is a comparison, not a decision, and the next
        reader should know which it is."""
        post = new(env, tag=self.a_mention(env.local.ap_profile_id.upper()))
        assert post is not None
        assert Notification.query.filter_by(user_id=env.local.id).count() == 0

    def test_somebody_who_has_blocked_the_author_is_not_notified(self, env):
        db.session.add(UserBlock(blocker_id=env.local.id,
                                 blocked_id=env.author.id))
        db.session.commit()
        post = new(env, tag=self.a_mention(env.local.ap_profile_id))
        assert post is not None
        assert Notification.query.filter_by(user_id=env.local.id).count() == 0

    def test_a_mention_of_somebody_who_is_not_here(self, env):
        post = new(env, tag=self.a_mention(
            f"{current_app.config['SERVER_URL']}/u/nobody"))
        assert post is not None
        assert Notification.query.count() == 0

    def test_a_mention_of_a_remote_actor_is_not_ours_to_notify(self, env):
        post = new(env, tag=self.a_mention('https://other.test/u/bob'))
        assert post is not None
        assert Notification.query.count() == 0

    def test_a_mention_with_no_href(self, env):
        post = new(env, tag=[{'type': 'Mention'}])
        assert post is not None

    def test_a_mention_whose_href_is_not_a_string(self, env):
        post = new(env, tag=[{'type': 'Mention', 'href': 5}])
        assert post is not None

    def test_two_mentions_notify_both(self, env):
        second = make_user(env.baseline.instance_local, 'another', local=True)
        second.ap_profile_id = f"{current_app.config['SERVER_URL']}/u/another"
        db.session.commit()
        new(env, tag=[{'type': 'Mention', 'href': env.local.ap_profile_id},
                      {'type': 'Mention', 'href': second.ap_profile_id}])
        assert Notification.query.count() == 2

    def test_a_tag_list_that_is_not_a_list(self, env):
        assert new(env, tag={'type': 'Mention'}) is not None


class TestAQuestionFromAPeer:
    """D1330. Four reads, four ways to lose the post."""

    WELL_FORMED = {'type': 'Question', 'endTime': '2026-02-01T00:00:00Z'}

    def test_a_single_choice_poll(self, env):
        post = new(env, **self.WELL_FORMED,
                   oneOf=[{'name': 'yes'}, {'name': 'no'}])
        assert post.type == POST_TYPE_POLL
        poll = Poll.query.filter_by(post_id=post.id).one()
        assert poll.mode == 'single'
        assert [choice.choice_text for choice
                in PollChoice.query.filter_by(post_id=post.id)
                .order_by(PollChoice.sort_order)] == ['yes', 'no']

    def test_a_multiple_choice_poll(self, env):
        post = new(env, **self.WELL_FORMED, anyOf=[{'name': 'yes'}])
        assert Poll.query.filter_by(post_id=post.id).one().mode == 'multiple'

    def test_the_end_time_is_stored_as_a_moment(self, env):
        post = new(env, **self.WELL_FORMED, oneOf=[{'name': 'yes'}])
        poll = Poll.query.filter_by(post_id=post.id).one()
        assert poll.end_poll.year == 2026
        assert poll.end_poll.month == 2

    @pytest.mark.parametrize('overrides,reason', [
        ({'type': 'Question', 'oneOf': [{'name': 'yes'}]}, 'no endTime'),
        ({'type': 'Question', 'endTime': 'not a date',
          'oneOf': [{'name': 'yes'}]}, 'an endTime that is not a date'),
        ({'type': 'Question', 'endTime': 5, 'oneOf': [{'name': 'yes'}]},
         'an endTime that is not a string'),
        ({'type': 'Question', 'endTime': '2026-02-01T00:00:00Z'},
         'neither oneOf nor anyOf'),
        ({'type': 'Question', 'endTime': '2026-02-01T00:00:00Z', 'oneOf': [{}]},
         'a choice with no name'),
        ({'type': 'Question', 'endTime': '2026-02-01T00:00:00Z', 'oneOf': []},
         'an empty choice list'),
        ({'type': 'Question', 'endTime': '2026-02-01T00:00:00Z',
          'oneOf': 'yes'}, 'choices that are not a list'),
        ({'type': 'Question', 'endTime': '2026-02-01T00:00:00Z',
          'oneOf': [{'name': '   '}]}, 'a choice named only spaces'),
        ({'type': 'Question', 'endTime': '2026-02-01T00:00:00Z',
          'oneOf': [{'name': 5}]}, 'a choice whose name is a number'),
    ])
    def test_a_question_it_cannot_read_still_lands_as_a_post(self, env,
                                                            overrides, reason):
        """The post is what matters: it stays an ordinary post rather than
        becoming a poll with nothing in it."""
        post = new(env, **overrides)
        assert post is not None, reason
        assert post.type == POST_TYPE_ARTICLE
        assert Poll.query.filter_by(post_id=post.id).count() == 0
        assert PollChoice.query.filter_by(post_id=post.id).count() == 0

    def test_choices_given_as_bare_strings_are_taken(self, env):
        """Deliberately lenient: a list of strings names the choices as clearly
        as a list of dicts does."""
        post = new(env, **self.WELL_FORMED, oneOf=['yes', 'no'])
        assert post.type == POST_TYPE_POLL
        assert [choice.choice_text for choice
                in PollChoice.query.filter_by(post_id=post.id)
                .order_by(PollChoice.sort_order)] == ['yes', 'no']

    def test_the_unusable_choices_in_a_mixed_list_are_skipped(self, env):
        post = new(env, **self.WELL_FORMED,
                   oneOf=[{'name': 'yes'}, {}, 5, {'name': 'no'}])
        assert [choice.choice_text for choice
                in PollChoice.query.filter_by(post_id=post.id)
                .order_by(PollChoice.sort_order)] == ['yes', 'no']

    def test_the_sort_order_has_no_gaps(self, env):
        """It is `enumerate` over what was kept, not over what arrived, so a
        skipped choice does not leave a hole the UI has to cope with."""
        post = new(env, **self.WELL_FORMED,
                   oneOf=[{}, {'name': 'yes'}, {}, {'name': 'no'}])
        assert sorted(choice.sort_order for choice
                      in PollChoice.query.filter_by(post_id=post.id)) == [1, 2]

    def test_a_poll_that_has_already_closed_is_still_a_poll(self, env):
        """Unlike a ban's expiry, a past end time is meaningful here."""
        post = new(env, type='Question', endTime='2020-01-01T00:00:00Z',
                   oneOf=[{'name': 'yes'}])
        assert post.type == POST_TYPE_POLL


class TestTheEndTimeParser:
    @pytest.mark.parametrize('value', [
        '2026-02-01T00:00:00Z',
        '2026-02-01T00:00:00+00:00',
        '2026-02-01T00:00:00',
        '2026-02-01',
    ])
    def test_a_shape_it_reads(self, value):
        assert parse_poll_end_time(value) is not None

    @pytest.mark.parametrize('value', [
        'not a date', '', None, 5, [], {}, '2026-13-45T99:99:99Z', 'Z',
    ])
    def test_a_shape_it_refuses(self, value):
        assert parse_poll_end_time(value) is None

    def test_it_never_raises(self):
        for value in (object(), b'2026-02-01', ['2026-02-01'], {'a': 1}, 0, -1):
            assert parse_poll_end_time(value) is None

    def test_an_offset_is_converted_to_utc_and_dropped(self):
        """Asserted on the RETURN VALUE, not through the database.

        `Poll.end_poll` is `timestamp without time zone`, so handing it an aware
        datetime leaves the conversion to the database session's own TimeZone --
        which is `Etc/UTC` under this harness, so a mutant that skips the
        conversion stores the same thing and passes every test that goes through
        a commit. The determinism is the point: the same document must mean the
        same instant on a server whose session TimeZone is not UTC.
        """
        from datetime import datetime as real_datetime

        converted = parse_poll_end_time('2027-01-01T12:00:00+05:00')
        assert converted == real_datetime(2027, 1, 1, 7, 0)
        assert converted.tzinfo is None

    def test_utc_and_a_naive_string_agree(self):
        from datetime import datetime as real_datetime

        for value in ('2027-01-01T12:00:00+00:00', '2027-01-01T12:00:00Z',
                      '2027-01-01T12:00:00'):
            parsed = parse_poll_end_time(value)
            assert parsed == real_datetime(2027, 1, 1, 12, 0), value
            assert parsed.tzinfo is None, value

    def test_a_negative_offset_moves_the_other_way(self):
        from datetime import datetime as real_datetime

        assert parse_poll_end_time('2027-01-01T12:00:00-05:00') == \
            real_datetime(2027, 1, 1, 17, 0)


class TestTheAiCheck:
    """D1331. The endpoint is the operator's, its answer is still a document from
    somewhere else."""

    @pytest.fixture
    def configured(self, env, monkeypatch):
        monkeypatch.setitem(current_app.config, 'DETECT_AI_ENDPOINT',
                            'https://ai.example/check')
        env.author.created = current_app.config and __import__(
            'app.models', fromlist=['utcnow']).utcnow()
        db.session.commit()
        return env

    def body(self):
        return {'name': 'a post', 'content': 'x' * 300}

    def _answer(self, http_mock, payload=None, text=None, status=200):
        route = 'https://ai.example/check'
        if text is not None:
            return http_mock.get(url__startswith=route).mock(
                return_value=httpx.Response(status, text=text))
        return http_mock.get(url__startswith=route).mock(
            return_value=httpx.Response(status, json=payload))

    def test_a_verdict_of_ai_marks_the_post(self, configured, http_mock):
        self._answer(http_mock, {'confidence': 0.9, 'detection_result': 'ai'})
        post = new(configured, **self.body())
        assert post.ai_generated is True

    def test_a_verdict_of_human_does_not(self, configured, http_mock):
        self._answer(http_mock, {'confidence': 0.9, 'detection_result': 'human'})
        post = new(configured, **self.body())
        assert post.ai_generated is False

    def test_a_confidence_below_the_threshold_is_ignored(self, configured,
                                                         http_mock):
        self._answer(http_mock, {'confidence': 0.5, 'detection_result': 'ai'})
        post = new(configured, **self.body())
        assert post.ai_generated is False

    @pytest.mark.parametrize('payload', [
        {}, {'confidence': 0.9}, {'detection_result': 'ai'},
        {'confidence': 'high', 'detection_result': 'ai'},
        {'confidence': None, 'detection_result': 'ai'},
        {'error': 'quota exceeded'},
        [], 'a string', 5,
    ])
    def test_an_answer_it_cannot_read_still_lands_the_post(self, configured,
                                                          http_mock, payload):
        self._answer(http_mock, payload)
        post = new(configured, **self.body())
        assert post is not None
        assert post.ai_generated is False

    def test_a_body_that_is_not_json_at_all(self, configured, http_mock):
        self._answer(http_mock, text='<html>gateway timeout</html>')
        post = new(configured, **self.body())
        assert post is not None
        assert post.ai_generated is False

    def test_a_failure_status_is_not_read(self, configured, http_mock):
        self._answer(http_mock, {'confidence': 0.9, 'detection_result': 'ai'},
                     status=500)
        post = new(configured, **self.body())
        assert post.ai_generated is False


class TestTheVerdictReader:
    """`ai_verdict` on its own, since both call sites now go through it."""

    class _Response:
        def __init__(self, payload=None, raises=False):
            self._payload = payload
            self._raises = raises

        def json(self):
            if self._raises:
                raise ValueError('not json')
            return self._payload

    def test_a_usable_answer(self):
        from app.models import ai_verdict
        assert ai_verdict(self._Response({'confidence': 0.9,
                                          'detection_result': 'ai'})) == \
            ('ai', 0.9)

    def test_an_integer_confidence(self):
        from app.models import ai_verdict
        assert ai_verdict(self._Response({'confidence': 1,
                                          'detection_result': 'human'})) == \
            ('human', 1)

    @pytest.mark.parametrize('payload', [
        {}, {'confidence': 0.9}, {'detection_result': 'ai'},
        {'confidence': 'high', 'detection_result': 'ai'},
        {'confidence': None, 'detection_result': 'ai'},
        {'confidence': 0.9, 'detection_result': None},
        {'confidence': 0.9, 'detection_result': 5},
        [], 'a string', 5, None,
    ])
    def test_an_answer_it_refuses(self, payload):
        from app.models import ai_verdict
        assert ai_verdict(self._Response(payload)) is None

    def test_a_confidence_of_true_is_not_certainty(self):
        """`True` is an `int` in Python and `True > 0.8`, so a boolean would
        otherwise read as a confident verdict."""
        from app.models import ai_verdict
        assert ai_verdict(self._Response({'confidence': True,
                                          'detection_result': 'ai'})) is None

    def test_a_body_that_is_not_json(self):
        from app.models import ai_verdict
        assert ai_verdict(self._Response(raises=True)) is None


class TestTheSameCheckOnAComment:
    """`PostReply.new` carries the same block, and it had no guard at all."""

    @pytest.fixture
    def configured(self, env, monkeypatch):
        from app.models import utcnow
        monkeypatch.setitem(current_app.config, 'DETECT_AI_ENDPOINT',
                            'https://ai.example/check')
        env.author.created = utcnow()
        db.session.commit()
        return env

    def _answer(self, http_mock, payload=None, text=None):
        if text is not None:
            return http_mock.get(url__startswith='https://ai.example/check').mock(
                return_value=httpx.Response(200, text=text))
        return http_mock.get(url__startswith='https://ai.example/check').mock(
            return_value=httpx.Response(200, json=payload))

    def a_reply(self, env):
        from app.models import PostReply
        post = new(env, name='a post')
        return PostReply.new(env.author, post, in_reply_to=None, body='x' * 300,
                             body_html='<p>' + 'x' * 300 + '</p>',
                             notify_author=False, language_id=None,
                             distinguished=False, answer=None,
                             request_json={'id': 'https://remote.test/c/2',
                                           'object': {
                                 'id': 'https://remote.test/r/1',
                                 'type': 'Note',
                                 'attributedTo': AUTHOR,
                                 'to': [PUBLIC],
                                 'inReplyTo': post.ap_id,
                                 'content': 'x' * 300}})

    @pytest.mark.parametrize('payload', [
        {}, {'confidence': 0.9}, {'confidence': 'high', 'detection_result': 'ai'},
        [], 'a string',
    ])
    def test_an_answer_it_cannot_read_still_lands_the_comment(self, configured,
                                                             http_mock, payload):
        self._answer(http_mock, payload)
        assert self.a_reply(configured) is not None

    def test_a_body_that_is_not_json(self, configured, http_mock):
        self._answer(http_mock, text='<html>gateway timeout</html>')
        assert self.a_reply(configured) is not None

    def test_a_usable_answer_is_recorded(self, configured, http_mock):
        self._answer(http_mock, {'confidence': 0.9, 'detection_result': 'ai'})
        assert self.a_reply(configured) is not None


class TestAPostWithNoBodyAtAll:
    """D1332. `len(post.body) > 250` gates the AI check, and `post.body` is None
    for a post that carries no text -- a link post, an image post with nothing
    written under it. `TypeError: object of type 'NoneType' has no len()`, and the
    post is lost.

    Only an instance with DETECT_AI_ENDPOINT configured reaches that line, which
    is why it survived: the gate's FIRST operand is what hides the other two.
    """

    @pytest.fixture
    def configured(self, env, monkeypatch):
        from app.models import utcnow
        monkeypatch.setitem(current_app.config, 'DETECT_AI_ENDPOINT',
                            'https://ai.example/check')
        env.author.created = utcnow()
        db.session.commit()
        return env

    def test_a_post_with_no_body_still_arrives(self, configured):
        post = new(configured, name='a title and nothing else')
        assert post is not None
        assert post.body is None or post.body == ''

    def test_a_post_with_a_short_body_still_arrives(self, configured):
        post = new(configured, name='a post', content='too short to check')
        assert post is not None

    def test_the_check_is_not_run_for_a_body_that_is_too_short(self, configured,
                                                              http_mock):
        """No route is registered, so reaching the endpoint would raise."""
        assert new(configured, name='a post', content='short') is not None
        assert len(http_mock.calls) == 0

    def test_a_comment_with_no_body_is_the_same_shape(self, configured):
        """`PostReply.new`'s copy of the gate reads `len(reply.body)`."""
        from app.models import PostReply

        post = new(configured, name='a post')
        reply = PostReply.new(configured.author, post, in_reply_to=None, body=None,
                              body_html='', notify_author=False, language_id=None,
                              distinguished=False, answer=None,
                              request_json={'id': 'https://remote.test/c/3',
                                            'object': {
                                                'id': 'https://remote.test/r/2',
                                                'type': 'Note',
                                                'attributedTo': AUTHOR,
                                                'to': [PUBLIC],
                                                'inReplyTo': post.ap_id,
                                                'content': ''}})
        assert reply is not None

    def test_an_instance_with_no_endpoint_configured_never_looks(self, env,
                                                                 http_mock):
        """The first operand, asserted so the guard above is known to be the
        reason and not the configuration."""
        from app.models import utcnow
        env.author.created = utcnow()
        db.session.commit()
        assert new(env, name='a post', content='x' * 300) is not None
        assert len(http_mock.calls) == 0


class TestACommentWhoseBodyIsNull:
    """D1333. `body` reaches `PostReply.new` from a peer's Note, and
    `request_json['object']['source']['content']` is whatever the peer put there.
    `null` gave `body = None`, and `reply_is_just_link_to_gif_reaction(body)` --
    two lines into the method -- did `body.strip()`.

    Reached here through `app/activitypub/util.py`'s reply path, so the value is
    the one a peer actually supplies rather than one invented for the test.
    """

    def test_the_helpers_refuse_a_body_that_is_not_a_string(self):
        from app.utils import (reply_is_just_link_to_gif_reaction,
                               reply_is_low_effort)

        for value in (None, 5, [], {}, b'bytes'):
            assert reply_is_just_link_to_gif_reaction(value) is False
            assert reply_is_low_effort(value) is False

    def test_they_still_recognise_what_they_are_for(self):
        from app.utils import (reply_is_just_link_to_gif_reaction,
                               reply_is_low_effort)

        assert reply_is_just_link_to_gif_reaction(
            '  https://media.tenor.com/abc.gif ') is True
        assert reply_is_just_link_to_gif_reaction('a real comment') is False
        assert reply_is_low_effort('  This. ') is True
        assert reply_is_low_effort('a real comment') is False

    def test_a_comment_with_a_null_source_content_still_lands(self, env):
        """The whole path: a peer's Note whose `source.content` is null."""
        from app.activitypub.util import notify_about_post_reply  # noqa: F401
        from app.models import PostReply

        post = new(env, name='a post')
        reply = PostReply.new(env.author, post, in_reply_to=None, body=None,
                              body_html='', notify_author=False, language_id=None,
                              distinguished=False, answer=None,
                              request_json={'id': 'https://remote.test/c/9',
                                            'object': {
                                                'id': 'https://remote.test/r/9',
                                                'type': 'Note',
                                                'attributedTo': AUTHOR,
                                                'to': [PUBLIC],
                                                'inReplyTo': post.ap_id,
                                                'source': {'content': None}}})
        assert reply is not None
        assert reply.post_id == post.id


class TestTheAuthorTheNotificationNames:
    """The `if author is None: continue` beside the repaired lookup.

    `post.user_id` is the user the post was just written with, and a foreign key
    stands behind it, so a missing row cannot happen from the database's side --
    which is exactly why the old `.first()` was never noticed. The guard is
    asserted by simulating the absence at the session, the way
    `tests/test_ap_reports_and_undo_votes.py` does for its own unreachable arm
    (fact 653), rather than by pretending the schema can produce it.
    """

    def test_a_notification_is_not_written_without_an_author(self, env,
                                                             monkeypatch):
        from app import db as real_db

        original_get = real_db.session.get

        def get_without_the_author(model, identity, *args, **keywords):
            if model is User and identity == env.author.id:
                return None
            return original_get(model, identity, *args, **keywords)

        post = None
        monkeypatch.setattr(real_db.session, 'get', get_without_the_author)
        post = new(env, tag=[{'type': 'Mention',
                              'href': env.local.ap_profile_id}])
        monkeypatch.undo()

        assert post is not None
        assert Notification.query.filter_by(user_id=env.local.id).count() == 0
