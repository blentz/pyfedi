"""Round 273: the remaining one-line arms, and a paginator's backwards half.

    a hard purge         `User.purge_content(soft=False)` DELETES replies rather than flagging
                         them. Both arms are a moderation decision, and only the soft one had a row.
    a JWT with no secret `encode_jwt_token` raises rather than signing with nothing -- the token is
                         what the API accepts as an identity, so an unsigned one is an auth bypass.
    the modlog's guard   `add_to_modlog` refuses an action its own map does not name, which is what
                         keeps the log's vocabulary closed.
    an actor's handle    `User.mention_tag` and `Feed.lemmy_link`, each with a local and a remote
                         spelling. These are what a peer copies to address somebody.
    a broken feed chain  `Feed.path`'s `break` for a parent row that is gone.
    a page that is not HTML   `parse_page` refusing to run BeautifulSoup over a PDF.
    the paginator        `has_prev` and `prev_bookmark`, the backwards half of the keyset paginator,
                         whose forwards half is covered and whose backwards half is what the
                         "previous page" link reads.
    three empty answers  `user_notes`, `favorite_communities` and `following_user_ids` for an
                         account id that is absent or zero.
"""
from unittest.mock import patch

import httpx
import pytest
from flask import current_app, g

from app import db
from app.models import (Community, Feed, Post, PostReply, Site, User, UserNote)
from app.utils import (add_to_modlog, favorite_communities, following_user_ids, parse_page,
                       user_notes)
from tests.factories import (make_community, make_community_member, make_feed, make_instance,
                             make_post, make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('tailend')
    author = make_user(api_baseline.instance_local, 'tailendauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# Purging somebody's content
# --------------------------------------------------------------------------


class TestPurgingSomebodysReplies:
    """`purge_content(soft=...)` is called when an account is banned or deletes itself. The two arms
    are different moderation outcomes: a soft purge leaves the rows so a mistake can be undone, and
    a hard one removes them. `process_delete_request` calls it for a self-delete, where the account
    asked for the content to be gone.
    """

    def _reply(self, env, body, suffix='1'):
        """The reply is on SOMEBODY ELSE's post.

        On the purged account's own post the two arms cannot be told apart: `purge_content` deletes
        the post, and the reply goes with it by cascade whether this line flagged it or deleted it.
        """
        other = make_user(env.baseline.instance_local, f'otherposter{suffix}', local=True)
        db.session.commit()
        post = make_post(env.community, other,
                         ap_id=f'https://test.piefed.local/pc/{suffix}')
        db.session.commit()
        reply = make_post_reply(post, env.author, body=body)
        db.session.commit()
        return reply

    def test_a_hard_purge_removes_the_reply_row(self, env):
        """`else: db.session.delete(reply)`. Nothing is left to undelete, which is what somebody
        asking for their own content to be removed means."""
        reply = self._reply(env, 'to be removed', suffix='hard')
        reply_id = reply.id

        env.author.purge_content(soft=False)
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(PostReply, reply_id) is None

    def test_a_soft_purge_only_flags_it(self, env):
        """The other arm, and the default: the row survives so a wrong ban can be undone."""
        reply = self._reply(env, 'to be flagged', suffix='soft')
        reply_id = reply.id

        env.author.purge_content(soft=True)
        db.session.commit()

        db.session.expire_all()
        stored = db.session.get(PostReply, reply_id)
        assert stored is not None
        assert stored.deleted is True


class TestSigningAnApiToken:

    def test_an_instance_with_no_secret_key_refuses_to_sign(self, env, monkeypatch):
        """`if not current_app.config['SECRET_KEY']: raise`. The token this returns is what the
        alpha API accepts as an identity, so signing with an empty key would produce tokens ANY
        instance could forge -- the refusal is the only thing between a missing config value and an
        auth bypass."""
        monkeypatch.setitem(current_app.config, 'SECRET_KEY', '')

        with pytest.raises(Exception, match='SECRET_KEY'):
            env.author.encode_jwt_token()

    def test_a_configured_instance_signs_a_token(self, env):
        """The control, and it asserts the claims: `sub` is the account, `iss` this instance, and
        `jti` a unique id -- the comment on that field says it is there for revocation."""
        import jwt

        token = env.author.encode_jwt_token()
        payload = jwt.decode(token, current_app.config['SECRET_KEY'], algorithms=['HS256'])

        assert payload['sub'] == str(env.author.id)
        assert payload['iss'] == current_app.config['SERVER_NAME']
        assert payload['jti']


class TestWhatTheModerationLogAccepts:

    def test_an_action_the_map_does_not_name_is_refused(self, env):
        """`if action not in ModLog.action_map.keys(): raise`. The map is what renders each entry's
        text, so an action outside it would be a log line nobody can read -- and the log is what an
        instance's moderation is audited from."""
        with pytest.raises(Exception, match='Invalid action'):
            add_to_modlog('invent_a_new_action', actor=env.author)

    def test_a_known_action_is_recorded(self, env):
        from app.models import ModLog

        add_to_modlog('delete_post', actor=env.author, reason='a probe')
        db.session.commit()

        assert ModLog.query.filter_by(action='delete_post').count() == 1


# --------------------------------------------------------------------------
# How somebody is addressed
# --------------------------------------------------------------------------


class TestHowAnActorIsAddressed:

    def test_a_remote_accounts_mention_names_its_own_host(self, env):
        """`'@' + user_name + '@' + ap_domain`. This is the handle a reader copies to mention
        somebody, so the host has to be THEIRS -- naming this instance instead would address an
        account that does not exist."""
        remote = make_user(env.baseline.instance_remote, 'faraway')

        assert remote.mention_tag() == '@faraway@remote.piefed.test'

    def test_a_local_accounts_mention_names_this_instance(self, env):
        assert env.author.mention_tag() == \
            f'@{env.author.user_name}@{current_app.config["SERVER_NAME"]}'

    def test_a_remote_feeds_link_is_its_own_ap_id(self, env):
        """`Feed.lemmy_link`'s remote arm, lower-cased. The `~` prefix is the feed sigil, as `!` is
        a community's."""
        feed = make_feed(env.baseline.instance_remote, 'remotenews')
        feed.ap_id = 'News@Remote.Piefed.Test'
        db.session.commit()

        assert feed.lemmy_link() == '~news@remote.piefed.test'

    def test_a_local_feeds_link_names_this_instance(self, env):
        feed = make_feed(env.baseline.instance_local, 'localnews')
        feed.ap_id = None
        db.session.commit()

        assert feed.lemmy_link() == \
            f'~{feed.name}@{current_app.config["SERVER_NAME"]}'


class TestAFeedsPath:

    def test_a_child_feed_is_pathed_under_its_parent(self, env):
        """`path()` builds the url a feed is served at, parent-first."""
        parent = make_feed(env.baseline.instance_local, 'parentthere')
        parent.machine_name = 'parentthere'
        child = make_feed(env.baseline.instance_local, 'childthere')
        child.machine_name = 'childthere'
        db.session.commit()
        child.parent_feed_id = parent.id
        db.session.commit()

        assert child.path() == 'parentthere/childthere'

    def test_a_top_level_feed_is_its_own_path(self, env):
        """The loop not running at all, which is most feeds.

        `if parent_feed is None: break` inside it is UNREACHABLE and is recorded here rather than
        chased: `feed.parent_feed_id` is a foreign key to `feed.id` with no cascade, so the database
        refuses to delete a parent that a child still names --
        `psycopg2.errors.ForeignKeyViolation: update or delete on table "feed" violates foreign key
        constraint "feed_parent_feed_id_fkey"`, measured. There is no state in which the id is set
        and the row is gone. Fact 781's shape.
        """
        feed = make_feed(env.baseline.instance_local, 'aloneatthetop')
        feed.machine_name = 'aloneatthetop'
        feed.parent_feed_id = None
        db.session.commit()

        assert feed.path() == 'aloneatthetop'


# --------------------------------------------------------------------------
# A page that is not a page
# --------------------------------------------------------------------------


class TestReadingARemotePage:

    def test_something_that_is_not_html_is_refused(self, env):
        """`if 'text/html' not in response.headers.get('Content-Type', '')`. `parse_page` runs
        BeautifulSoup over whatever comes back to find OpenGraph tags, and it is handed a url a USER
        or a PEER supplied -- so a PDF or a video would otherwise be parsed as markup, which is slow
        and answers nothing."""
        with patch('app.utils.get_request',
                   return_value=httpx.Response(200, content=b'%PDF-1.4',
                                               headers={'Content-Type': 'application/pdf'},
                                               request=httpx.Request('GET', 'https://x.example/a.pdf'))):
            assert parse_page('https://x.example/a.pdf') is False

    def test_a_page_with_no_content_type_is_refused_too(self, env):
        """`.get('Content-Type', '')` -- the default is what stops a missing header being an
        AttributeError instead of a refusal."""
        with patch('app.utils.get_request',
                   return_value=httpx.Response(200, content=b'<html></html>', headers={},
                                               request=httpx.Request('GET', 'https://x.example/a'))):
            assert parse_page('https://x.example/a') is False

    def test_a_page_that_is_html_is_read(self, env):
        html = (b'<html><head><meta property="og:title" content="A title">'
                b'</head><body></body></html>')

        with patch('app.utils.get_request',
                   return_value=httpx.Response(200, content=html,
                                               headers={'Content-Type': 'text/html'},
                                               request=httpx.Request('GET', 'https://x.example/a'))):
            tags = parse_page('https://x.example/a')

        assert tags['og:title'] == 'A title'


# --------------------------------------------------------------------------
# The paginator's backwards half
# --------------------------------------------------------------------------


class TestThePaginatorsBackwardsHalf:
    """`SqlKeysetPagination` wraps a `sqlakeyset` page so it reads like Flask's paginator. Its
    forwards half is covered through the alpha API; `has_prev` and `prev_bookmark` are what a
    "previous page" link reads, and a wrong answer there is a link that either is not drawn or leads
    nowhere.

    Driven through `sqlakeyset` itself rather than a double, because what is being asserted is the
    mapping onto `paging.has_previous` / `paging.bookmark_previous` -- a hand-made page object would
    assert the test's own spelling of those names.
    """

    @pytest.fixture
    def rows(self, env):
        for index in range(5):
            make_post(env.community, env.author,
                      ap_id=f'https://test.piefed.local/pg/{index}')
        db.session.commit()

    def _page(self, env, bookmark=None):
        from sqlakeyset import get_page

        from app.utils import SqlKeysetPagination

        query = db.session.query(Post).filter_by(community_id=env.community.id) \
            .order_by(Post.id)
        return SqlKeysetPagination(get_page(query, per_page=2, page=bookmark))

    def test_the_first_page_has_no_previous(self, env, rows):
        page = self._page(env)

        assert page.has_next is True
        assert page.has_prev is False
        assert page.prev_bookmark is None

    def test_a_later_page_has_one_that_pages_back(self, env, rows):
        """`prev_bookmark` has to be the BACKWARDS bookmark, which `bookmark_next` also is not-None
        for -- so the assertion is that following it returns the rows of the page before, not merely
        that a value exists."""
        first = self._page(env)
        second = self._page(env, bookmark=first.next_bookmark)

        assert second.has_prev is True
        assert second.prev_bookmark is not None

        back = self._page(env, bookmark=second.prev_bookmark)
        assert [post.id for post in back.items] == [post.id for post in first.items]

    def test_the_last_page_has_no_next_bookmark(self, env, rows):
        """`next_bookmark`'s `if self.has_next else None`, the forwards twin of the arm above."""
        page = self._page(env, bookmark=self._page(env, bookmark=self._page(env).next_bookmark).next_bookmark)

        assert page.has_next is False
        assert page.next_bookmark is None


# --------------------------------------------------------------------------
# Three empty answers
# --------------------------------------------------------------------------


class TestThreeReadersAskedAboutNobody:

    def test_notes_for_no_account_are_empty(self, env):
        """`user_notes` is read per row of every listing, so the empty DICT for an anonymous visitor
        is what keeps a template's `.get()` working rather than raising.

        ALL THREE GUARDS IN THIS CLASS ARE EQUIVALENT MUTANTS, for one reason in three shapes:
        `UserNote.user_id`, `CommunityFavorite.user_id` and `UserFollower.local_user_id` are foreign
        keys to `user.id`, so a query for NULL or 0 matches nothing and the answer is the same empty
        collection either way. Each guard is kept for the query it saves -- these are read per row of
        every listing page -- and each row asserts the answer rather than the shortcut. Fact 1043's
        argument.
        """
        assert user_notes(None) == {}

    def test_a_note_is_keyed_by_the_account_it_is_about(self, env):
        other = make_user(env.baseline.instance_local, 'notedabout', local=True)
        db.session.commit()
        db.session.add(UserNote(user_id=env.author.id, target_id=other.id,
                                body='a private note'))
        db.session.commit()

        assert user_notes(env.author.id) == {other.id: 'a private note'}

    def test_favourites_for_no_account_are_empty(self, env):
        assert favorite_communities(None) == []

    def test_followed_ids_for_an_anonymous_visitor_are_empty(self, env):
        """`if user_id == 0` -- `User.get_id()` answers 0 for an anonymous visitor, which is the
        value this reader is actually handed."""
        assert following_user_ids(0) == []
