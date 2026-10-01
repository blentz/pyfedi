"""A user's keyword content filters, on a post and on a reply.

`Post.blocked_by_content_filter` is the live one: `app/api/alpha/views.py` and the
three post-teaser templates call it, and it answers the filter's NAME when the
content matches so the template can say which filter hid it.

`PostReply.blocked_by_content_filter` had no callers until R168 wired it up (see
tests/test_reply_content_filters.py). D1364 found it disagreeing with
the live one four ways, and because nothing calls it the disagreement could be settled
by making it match rather than left as a product question:

    filter {'spoilers': ['ass']}, content 'a classic passage'
        post  False        tokenizes on \\w+ and matches whole words
        reply 'spoilers'   matched any substring, so that filter hid every reply
                           containing "class" or "passage"
    keyword 'Ass'
        post  'spoilers'   lowercases each keyword
        reply False        did not, so a filter typed with a capital did nothing
    the viewer is the author
        post  False        exempts your own content
        reply n/a          took no user_id, so your own reply could be hidden
    body is NULL
        reply AttributeError: 'NoneType' object has no attribute 'lower'

The last one is why this mattered beyond tidiness: `PostReply.body` is nullable and a
peer's `source.content` of null lands there (D1333), so wiring reply filtering up
would have crashed the page for every filtering user who met one.
"""
import pytest
from flask import g

from app import db
from app.models import Post, PostReply, Site


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    db.session.commit()
    post = db.session.get(Post, api_baseline.post1.id)
    reply = db.session.get(PostReply, api_baseline.reply1.id)
    return SimpleNamespace(baseline=api_baseline, post=post, reply=reply,
                           # a viewer who is NOT the author of either
                           viewer=api_baseline.user1.id)


SPOILERS = {'spoilers': ['ass']}


def post_says(env, title, filters=SPOILERS, viewer=None):
    env.post.title = title
    db.session.commit()
    return env.post.blocked_by_content_filter(
        filters, env.viewer if viewer is None else viewer)


def reply_says(env, body, filters=SPOILERS, viewer=None):
    env.reply.body = body
    db.session.commit()
    return env.reply.blocked_by_content_filter(
        filters, env.viewer if viewer is None else viewer)


class TestWhatAFilterMatches:
    @pytest.mark.parametrize('content', ['ass', 'an ass', 'ASS', 'Ass here',
                                         'one ass two'])
    def test_a_whole_word_is_filtered(self, env, content):
        assert post_says(env, content) == 'spoilers'
        assert reply_says(env, content) == 'spoilers'

    @pytest.mark.parametrize('content', ['a classic passage', 'classic', 'passage',
                                         'assorted', 'brass'])
    def test_a_substring_is_not(self, env, content):
        """The divergence that hid every reply containing "class"."""
        assert post_says(env, content) is False
        assert reply_says(env, content) is False

    def test_punctuation_does_not_hide_a_match(self, env):
        """`\\w+` splits on it, so the word is still a token."""
        assert post_says(env, 'what an (ass).') == 'spoilers'
        assert reply_says(env, 'what an (ass).') == 'spoilers'

    def test_an_uppercase_keyword_still_matches(self, env):
        """The reply side never lowercased the keyword, so a filter typed
        "Spoiler" silently did nothing."""
        filters = {'spoilers': ['Ass']}

        assert post_says(env, 'ass', filters) == 'spoilers'
        assert reply_says(env, 'ass', filters) == 'spoilers'

    def test_the_name_of_the_matching_filter_comes_back(self, env):
        """Not True: the template shows which filter hid the content."""
        filters = {'sport': ['football'], 'spoilers': ['ending']}

        assert post_says(env, 'the ending', filters) == 'spoilers'
        assert reply_says(env, 'the football', filters) == 'sport'

    def test_the_first_matching_filter_wins(self, env):
        filters = {'a': ['word'], 'b': ['word']}

        assert post_says(env, 'word', filters) in ('a', 'b')
        assert reply_says(env, 'word', filters) in ('a', 'b')

    @pytest.mark.parametrize('filters', [None, {}, {'empty': []}])
    def test_no_filters_means_nothing_is_hidden(self, env, filters):
        assert post_says(env, 'ass', filters) is False
        assert reply_says(env, 'ass', filters) is False


class TestYourOwnContent:
    """`if self.user_id == user_id: return False`. The reply version had no
    user_id parameter, so your own reply could be hidden from you."""

    def test_your_own_post_is_never_hidden(self, env):
        assert post_says(env, 'ass', viewer=env.post.user_id) is False

    def test_your_own_reply_is_never_hidden(self, env):
        assert reply_says(env, 'ass', viewer=env.reply.user_id) is False

    def test_somebody_elses_still_is(self, env):
        assert post_says(env, 'ass', viewer=env.viewer) == 'spoilers'
        assert reply_says(env, 'ass', viewer=env.viewer) == 'spoilers'

    def test_an_anonymous_viewer_sees_the_filter_applied(self, env):
        """`current_user.get_id()` is None for an anonymous reader, and a template
        passes that straight in. No author id matches None, so filtering applies --
        which is right, since an anonymous reader has no filters of their own and any
        that reach here came from somewhere else."""
        assert post_says(env, 'ass', viewer=None) == 'spoilers'
        assert reply_says(env, 'ass', viewer=None) == 'spoilers'


class TestAReplyWithNoBody:
    """`PostReply.body` is nullable, and a peer's `source.content` of null lands
    there (D1333). This was `AttributeError: 'NoneType' object has no attribute
    'lower'`."""

    def test_a_null_body_is_not_filtered(self, env):
        assert reply_says(env, None) is False

    def test_a_null_body_with_no_filters(self, env):
        assert reply_says(env, None, None) is False

    def test_an_empty_body(self, env):
        assert reply_says(env, '') is False


class TestThePostSideHasNoNullToWorryAbout:
    def test_an_empty_title(self, env):
        assert post_says(env, '') is False


class TestTheTwoAgree:
    """The property, and the point of the round: one filter, one meaning, whichever
    kind of content it is applied to."""

    CONTENT = ['ass', 'ASS', 'a classic passage', 'assorted', 'what an (ass).',
               'nothing here', '', 'ass ass ass', 'Ass']
    FILTERS = [SPOILERS, {'spoilers': ['Ass']}, {'x': ['ass', 'other']},
               {'a': ['nothing']}, None, {}]

    @pytest.mark.parametrize('content', CONTENT)
    @pytest.mark.parametrize('filters', FILTERS)
    def test_the_same_verdict_for_the_same_words(self, env, content, filters):
        assert post_says(env, content, filters) == reply_says(env, content, filters)
