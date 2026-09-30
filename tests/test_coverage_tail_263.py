"""Round 263: what a federated Create writes onto a post, and what it will not.

`Post.new` and `PostReply.new` in `app/models.py` are how every post and comment from another
server enters this database. Both are long, and the arms left uncovered are the ones that make a
decision ABOUT the incoming content rather than merely copying it:

    indexable       three separate writers set it False -- a private community, a
                    `searchableBy` the peer marked non-public, and (for a reply) the same pair.
                    It is what decides whether the row is offered to search, so each writer is
                    one line between "not indexed" and "indexed".
    nsfl / ai_generated    imposed by the COMMUNITY on every post in it, whatever the post said.
    edited_at       set from the activity's own `type`, which is what a reader sees as "edited".
    body            the `text/markdown` arm, one of four spellings a peer may send a body in.
    file_path       an attachment field that names a file already on this instance's disk.

Also here: the AI-detection call, whose `except` arm is what stops an unreachable endpoint
discarding a new account's first post (D1332's neighbour); the Event banner's `File`; the author's
own upvote and the cache it invalidates for a local author; and two arms of `PostReply.new` -- the
absent-`Site` default and the `IntegrityError` retry that answers with the reply another worker
committed.
"""
from unittest.mock import patch

import httpx
import pytest
from flask import current_app

from app import db
from app.activitypub.util import create_post
from app.constants import POST_TYPE_EVENT, POST_TYPE_IMAGE
from app.models import File, Post, PostReply, PostVote, Site
from app.utils import set_setting
from tests.factories import (make_community, make_instance, make_post, make_site, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
FOLLOWERS = 'https://m.example/users/alice/followers'
LONG_BODY = 'A sentence that has to be long enough to reach the AI check. ' * 6


@pytest.fixture
def ingest(db_session):
    """An author, a community, and the Site row `blocked_phrases()` reads.

    `set_setting('cache_remote_images_locally', False)` keeps `make_image_sizes` out of every row
    below. Precedent: `tests/test_event_post_type_survives_update.py:161-164`, and the standing
    warning in `tests/test_ap_update_post_tails.py` that the setting gates only the call sites
    that consult it -- `Post.new`'s does, at the `if post.image_id and ...` line.
    """
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('m.example'), 'alice')
    community = make_community()
    make_site()
    set_setting('cache_remote_images_locally', False)
    db.session.commit()
    return author, community


def create_activity(obj_extra=None, activity_type='Create', **activity_extra):
    """A Create wrapping a titled public object, which is the ordinary article shape."""
    activity = {
        'id': 'https://m.example/users/alice/statuses/1/activity',
        'type': activity_type,
        'object': {
            'id': 'https://m.example/users/alice/statuses/1',
            'type': 'Page',
            'name': 'An article from somewhere else',
            'content': '<p>hello world, at some length so a title can be derived</p>',
            'attributedTo': 'https://m.example/users/alice',
            'to': [PUBLIC],
            'cc': [FOLLOWERS],
            **(obj_extra or {}),
        },
    }
    activity.update(activity_extra)
    return activity


def ingested(ingest, obj_extra=None, **kwargs):
    author, community = ingest
    return create_post(False, community, create_activity(obj_extra, **kwargs), author)


# --------------------------------------------------------------------------
# What the community imposes on every post in it
# --------------------------------------------------------------------------


class TestWhatTheCommunityImposesOnAPost:
    """Four one-line writes, each overriding what the peer said about its own post. A community
    marked nsfw or nsfl is marked that way for readers who filtered it out, so an arm that does
    not fire shows the post to somebody who asked not to see it.
    """

    def test_an_nsfw_community_marks_the_post_nsfw(self, ingest):
        """The comment on this line names the reason: Lemmy below 0.19.8 let a post in an nsfw
        community be flagged sfw, so the community's own flag has to win."""
        author, community = ingest
        community.nsfw = True
        db.session.commit()

        post = ingested(ingest)

        assert post.nsfw is True

    def test_an_nsfl_community_marks_the_post_nsfl(self, ingest):
        author, community = ingest
        community.nsfl = True
        db.session.commit()

        post = ingested(ingest)

        assert post.nsfl is True

    def test_an_ai_generated_community_marks_the_post_too(self, ingest):
        author, community = ingest
        community.ai_generated = True
        db.session.commit()

        post = ingested(ingest)

        assert post.ai_generated is True

    def test_a_private_community_makes_the_post_unindexable(self, ingest):
        """`if community.private: post.indexable = False`. The author's own `indexable` set it
        True a few lines up, so this line is the only thing keeping a private community's posts
        out of search."""
        author, community = ingest
        community.private = True
        db.session.commit()

        post = ingested(ingest)

        assert post.indexable is False

    def test_an_ordinary_community_imposes_none_of_them(self, ingest):
        """The control for all four. Every flag is off on the community, so a post that arrives
        with them off keeps them off -- which is how a mutant that hard-codes one would show."""
        post = ingested(ingest)

        assert (post.nsfw, post.nsfl, post.ai_generated) == (False, False, False)
        assert post.indexable is True


class TestWhoMayIndexThePost:

    def test_a_non_public_searchable_by_makes_the_post_unindexable(self, ingest):
        """`searchableBy` is the peer's own statement about search, and anything other than the
        Public collection means no. The author's `indexable` is True, so this is the line that
        honours a remote author's choice."""
        post = ingested(ingest, {'searchableBy': FOLLOWERS})

        assert post.indexable is False

    def test_a_public_searchable_by_leaves_it_indexable(self, ingest):
        post = ingested(ingest, {'searchableBy': PUBLIC})

        assert post.indexable is True


class TestTheEditedMarker:

    def test_an_update_activity_marks_the_post_edited(self, ingest):
        """`if 'type' in request_json and request_json['type'] == 'Update'`. `Post.new` is
        reached with an Update when the post did not exist here yet -- a peer editing a post this
        instance never received -- and the timestamp is what a reader sees as "edited"."""
        post = ingested(ingest, activity_type='Update')

        assert post.edited_at is not None

    def test_a_create_activity_does_not(self, ingest):
        post = ingested(ingest)

        assert post.edited_at is None


# --------------------------------------------------------------------------
# The four spellings a body may arrive in
# --------------------------------------------------------------------------


class TestHowTheBodyIsRead:
    """`source` markdown, `text/html`, `text/markdown`, and bare content. Only the first two had
    rows; the third is Lemmy's older spelling and stores the peer's text UNRENDERED as `body`.
    """

    def test_a_markdown_media_type_keeps_the_markdown_as_the_body(self, ingest):
        post = ingested(ingest, {'mediaType': 'text/markdown',
                                 'content': 'a **bold** claim'})

        assert post.body == 'a **bold** claim'
        assert '<strong>bold</strong>' in post.body_html

    def test_an_html_media_type_stores_the_rendered_html(self, ingest):
        """The neighbouring arm, for contrast: here `body` is the TEXT and `body_html` is the
        peer's markup after `allowlist_html`."""
        post = ingested(ingest, {'mediaType': 'text/html',
                                 'content': '<p>a <strong>bold</strong> claim</p>'})

        assert 'bold' in post.body
        assert '<strong>bold</strong>' in post.body_html

    def test_a_markdown_source_wins_over_the_media_type(self, ingest):
        """D1346. `source` is read first, so a peer sending both gets its markdown stored rather
        than text recovered from its own HTML."""
        post = ingested(ingest, {
            'mediaType': 'text/html',
            'content': '<p>rendered</p>',
            'source': {'mediaType': 'text/markdown', 'content': 'the *real* source'}})

        assert post.body == 'the *real* source'

    def test_a_body_with_no_media_type_is_wrapped_and_allowlisted(self, ingest):
        """The `else`: no `mediaType` at all, which is what Mastodon sends."""
        post = ingested(ingest, {'content': 'bare text with no markup'})

        assert post.body_html.startswith('<p>')
        assert 'bare text' in post.body


# --------------------------------------------------------------------------
# An image attachment
# --------------------------------------------------------------------------


class TestAnImageAttachment:
    """`is_image_url` asks the peer with a HEAD request for a url whose extension it cannot
    judge, so every row here registers that response -- `assert_all_called` then makes the HEAD
    positive evidence that the image arm was entered at all.
    """

    IMAGE_URL = 'https://m.example/pictures/one.png'

    @pytest.fixture(autouse=True)
    def head_answers_png(self, http_mock):
        http_mock.head(self.IMAGE_URL).mock(
            return_value=httpx.Response(200, headers={'content-type': 'image/png'}))

    def _image_attachment(self, **extra):
        return {'attachment': [{'type': 'Image', 'url': self.IMAGE_URL, **extra}]}

    def test_a_file_path_on_the_attachment_is_stored(self, ingest):
        """`if file_path: image.file_path = file_path`. PieFed sends `file_path` when the image
        is already on the receiving side's own disk -- it is the local path the resize would
        otherwise have to produce, so without this line the same bytes are fetched again."""
        post = ingested(ingest, self._image_attachment(
            file_path='app/static/media/posts/ab/cd/already-here.png'))

        assert post.type == POST_TYPE_IMAGE
        stored = db.session.get(File, post.image_id)
        assert stored.file_path == 'app/static/media/posts/ab/cd/already-here.png'

    def test_an_attachment_with_no_file_path_stores_none(self, ingest):
        """The False side. `''` would be a path, and `File.delete_from_disk` reads this column,
        so the absent value has to stay absent."""
        post = ingested(ingest, self._image_attachment())

        assert db.session.get(File, post.image_id).file_path is None

    def test_the_alt_text_is_taken_from_the_attachment_name(self, ingest):
        post = ingested(ingest, self._image_attachment(name='a cat on a wall'))

        assert db.session.get(File, post.image_id).alt_text == 'a cat on a wall'


class TestAnEventsBanner:

    EVENT_KEYS = {
        'startTime': '2030-01-01T10:00:00',
        'endTime': '2030-01-01T12:00:00',
        'timezone': 'Europe/London',
        'maximumAttendeeCapacity': 50,
        'participantCount': 0,
        'onlineLink': '',
        'joinMode': 'free',
        'externalParticipationUrl': '',
        'anonymousParticipation': False,
        'isOnline': False,
        'buyTicketsLink': '',
        'feeCurrency': 'GBP',
        'feeAmount': 0,
        'location': {'type': 'Place', 'name': 'somewhere'},
    }

    def test_an_events_image_becomes_the_posts_file(self, ingest):
        """D1352's twin in the create path. An Event carries its banner as `image` rather than as
        an attachment, and the `post.image is None` test means an Event that already took an
        image from an attachment keeps that one."""
        post = ingested(ingest, {'type': 'Event',
                                 'image': {'type': 'Image',
                                           'url': 'https://m.example/banners/one.png'},
                                 **self.EVENT_KEYS})

        assert post.type == POST_TYPE_EVENT
        assert post.image_id is not None
        assert db.session.get(File, post.image_id).source_url == \
            'https://m.example/banners/one.png'

    def test_an_event_with_no_image_has_no_file(self, ingest):
        post = ingested(ingest, {'type': 'Event', **self.EVENT_KEYS})

        assert post.image_id is None
        assert File.query.count() == 0


# --------------------------------------------------------------------------
# The author's own upvote
# --------------------------------------------------------------------------


class TestTheAuthorsOwnUpvote:

    def test_the_author_gets_a_vote_row(self, ingest):
        post = ingested(ingest)

        vote = PostVote.query.filter_by(post_id=post.id).one()
        assert (vote.user_id, vote.author_id, vote.effect) == (post.user_id, post.user_id, 1)

    def test_a_local_authors_upvote_list_is_invalidated(self, db_session):
        """`if user.is_local(): cache.delete_memoized(recently_upvoted_posts, user.id)`.

        `recently_upvoted_posts` is what draws the arrow next to a post as already voted on, and
        it is memoized -- so without this line a local author's own new post shows an unvoted
        arrow until the memo expires. Only a local account has such a list, which is what the
        guard is for.
        """
        make_instance('test.piefed.local', software='piefed')
        local_instance = make_instance('localhost')
        author = make_user(local_instance, 'localalice', local=True)
        community = make_community()
        make_site()
        set_setting('cache_remote_images_locally', False)
        db.session.commit()
        invalidated = []

        with patch('app.models.cache.delete_memoized',
                   side_effect=lambda func, *args: invalidated.append(args)):
            post = create_post(False, community, create_activity(), author)

        assert post is not None
        assert (author.id,) in invalidated

    def test_a_remote_authors_list_is_not_touched(self, ingest):
        """The False side. A remote author has no page on this instance to invalidate, and the
        memo key would be one no reader ever reads."""
        author, community = ingest
        invalidated = []

        with patch('app.models.cache.delete_memoized',
                   side_effect=lambda func, *args: invalidated.append(args)):
            ingested(ingest)

        assert (author.id,) not in invalidated


# --------------------------------------------------------------------------
# The AI-detection endpoint
# --------------------------------------------------------------------------


class TestWhenTheAiDetectorCannotBeReached:
    """D1332 is the comment above this block: `len(post.body)` raised `TypeError` for a post with
    no body, and a new account's first link post was lost -- on an instance with AI detection
    configured, which is why it survived so long. The `except` arm here is the same shape one
    line down: the endpoint is a THIRD-PARTY HTTP service, and a post must not be lost because it
    is down.
    """

    @pytest.fixture
    def detecting(self, ingest, monkeypatch):
        monkeypatch.setitem(current_app.config, 'DETECT_AI_ENDPOINT',
                            'https://detector.example/check')
        return ingest

    def test_a_post_survives_an_endpoint_that_cannot_be_reached(self, detecting):
        """`except Exception: is_ai = None`. The post is stored and nothing is reported."""
        with patch('app.utils.get_request',
                   side_effect=ConnectionError('no route to host')):
            post = ingested(detecting, {'content': f'<p>{LONG_BODY}</p>'})

        assert post is not None
        assert db.session.get(Post, post.id).ai_generated is False

    def test_a_reply_survives_it_too(self, detecting):
        """`PostReply.new`'s copy of the same arm, which D1331 records as having had no guard of
        any kind."""
        author, community = detecting
        post = make_post(community, author, ap_id='https://m.example/p/1')
        db.session.commit()

        with patch('app.utils.get_request',
                   side_effect=ConnectionError('no route to host')):
            reply = PostReply.new(author, post, None, LONG_BODY, f'<p>{LONG_BODY}</p>',
                                  True, None, False, False,
                                  request_json={'id': 'https://m.example/activities/r1',
                                                'type': 'Create',
                                                'object': {'id': 'https://m.example/r/1'}})

        assert reply is not None
        assert reply.ap_id == 'https://m.example/r/1'

    def test_an_account_that_is_not_new_is_never_checked(self, detecting):
        """`user.created_very_recently()` is the first test in the chain, and it is what keeps
        this instance from sending every post it receives to a third party."""
        author, community = detecting
        author.created = None
        db.session.commit()
        calls = []

        with patch('app.utils.get_request', side_effect=lambda *a, **k: calls.append(a)):
            ingested(detecting, {'content': f'<p>{LONG_BODY}</p>'})

        assert calls == []


# --------------------------------------------------------------------------
# Two arms of PostReply.new
# --------------------------------------------------------------------------


class TestWhatAReplyDoesWithNoSiteRow:

    def test_the_reply_is_stored_when_there_is_no_site_row(self, db_session):
        """`site = db.session.get(Site, 1)` / `if site is None: site = Site()`.

        The two filters below it read `site.enable_gif_reply_rep_decrease` and
        `site.enable_this_comment_filter`, so without the default an instance mid-setup -- the
        row is created by `flask init-db`, and federation can arrive first -- raised
        `AttributeError` on None for every incoming reply. An unsaved `Site()` supplies the
        column defaults, so the filters read False and the reply is kept.
        """
        make_instance('test.piefed.local', software='piefed')
        author = make_user(make_instance('m.example'), 'alice')
        community = make_community()
        db.session.commit()
        post = make_post(community, author, ap_id='https://m.example/p/2')
        db.session.commit()
        assert db.session.get(Site, 1) is None

        reply = PostReply.new(author, post, None, 'a reply', '<p>a reply</p>',
                              True, None, False, False)

        assert reply is not None
        assert reply.body == 'a reply'

    def test_a_reply_the_comment_filters_look_at_is_stored_too(self, db_session):
        """Where `site = Site()` is OBSERVABLE, and the only place it is.

        The two filters below it are `predicate(reply.body) and site.<flag>`, so for an ordinary
        body Python short-circuits and `site` is never dereferenced -- the default cannot be told
        from no default at all. A body of `this` makes `reply_is_low_effort` true, the flag IS
        read, and the two answers diverge: the default reads a flag, no default raises
        `AttributeError` on None.

        What the default reads is `None` rather than the column's `default=False`: a SQLAlchemy
        column default is applied on INSERT, and this instance is never saved. The effective
        answer is the same -- the filter is off -- but by short-circuit rather than by the
        configured default.
        """
        make_instance('test.piefed.local', software='piefed')
        author = make_user(make_instance('m.example'), 'alice')
        community = make_community()
        db.session.commit()
        post = make_post(community, author, ap_id='https://m.example/p/4')
        db.session.commit()
        assert db.session.get(Site, 1) is None

        reply = PostReply.new(author, post, None, 'this', '<p>this</p>',
                              True, None, False, False)

        assert reply is not None
        assert reply.body == 'this'

    def test_a_post_is_stored_when_there_is_no_site_row(self, db_session):
        """D1429, which is what makes the guard above reachable at all.

        `blocked_phrases()` read `site.blocked_phrases` outright, and both `Post.new` and
        `PostReply.new` call it -- `PostReply.new` two lines below its own `site = Site()`
        default, so that default could only ever be reached by a reply with an EMPTY BODY. Every
        incoming post and reply WITH a body raised `AttributeError` instead.
        """
        make_instance('test.piefed.local', software='piefed')
        author = make_user(make_instance('m.example'), 'alice')
        community = make_community()
        set_setting('cache_remote_images_locally', False)
        db.session.commit()
        assert db.session.get(Site, 1) is None

        post = Post.new(author, community, create_activity())

        assert post is not None
        assert post.title == 'An article from somewhere else'

    def test_no_site_row_means_no_blocked_phrases(self, db_session):
        """The same repair at its own boundary. No row is the same answer as an empty setting --
        an empty list -- rather than an exception, because a phrase list nobody has configured
        cannot block anything."""
        from app.utils import blocked_phrases

        assert db.session.get(Site, 1) is None
        assert blocked_phrases() == []


class TestTwoWorkersDeliveringTheSameReply:

    def test_the_reply_another_worker_committed_is_returned(self, db_session):
        """`except IntegrityError: rollback; return PostReply.query.filter_by(ap_id=...).one()`.

        Two inbox workers handed the same Create -- an Announce and the original delivery, which
        is ordinary -- race on the unique `ap_id`. The loser must answer with the row the winner
        wrote rather than raising, because the caller federates on from what it is given.

        The race is reproduced by committing the row first and then asking `PostReply.new` for
        the same `ap_id`, which is the state the loser finds.
        """
        make_instance('test.piefed.local', software='piefed')
        author = make_user(make_instance('m.example'), 'alice')
        community = make_community()
        make_site()
        db.session.commit()
        post = make_post(community, author, ap_id='https://m.example/p/3')
        db.session.commit()
        request_json = {'id': 'https://m.example/activities/r2',
                        'type': 'Create',
                        'object': {'id': 'https://m.example/r/2'}}
        winner = PostReply.new(author, post, None, 'first in', '<p>first in</p>',
                               True, None, False, False, request_json=request_json)
        db.session.commit()

        loser = PostReply.new(author, post, None, 'second in', '<p>second in</p>',
                              True, None, False, False, request_json=request_json)

        assert loser.id == winner.id
        assert loser.body == 'first in'
        assert PostReply.query.filter_by(ap_id='https://m.example/r/2').count() == 1
