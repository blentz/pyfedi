"""Fetching a NodeBB topic's replies, and the timestamp a peer puts on a post.

Sub-project 124 -- `get_nodebb_replies_in_background` and the tail of
`resolve_remote_post` in `app/activitypub/util.py`.

NodeBB serves a topic as an OrderedCollection: the first entry is the post and the
rest are its replies. `resolve_remote_post` resolves the first, then hands the
slice to this function. Everything in that slice is a peer's choice, and nothing
looked at it (D1340):

* a string `orderedItems` iterates its CHARACTERS, so ten single-letter "uris"
  were fetched;
* None or a number is `TypeError: 'NoneType' object is not iterable`;
* one reply that failed abandoned the rest, because the `raise` left the loop.

The same function reads `totalItems` and `orderedItems` with full guards seventy
lines earlier and with none here, which is what made the pair worth reading
together.
"""
import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import get_nodebb_replies_in_background
from app.models import Community, Site
from tests.factories import (PEER_OBJECT_HOST as PEER_OBJECT_HOST_FOR_TESTS,
                             make_community, seed_community_owner)

PEER = 'peer.test'


@pytest.fixture
def env(app, db_session, site, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    seed_community_owner(PEER)
    community = make_community('news')
    db.session.commit()
    resolved = []

    import app.activitypub.util as ap_util
    monkeypatch.setattr(ap_util, 'resolve_remote_post',
                        lambda uri, community, *args, **keywords: resolved.append(uri))
    return SimpleNamespace(app=app, community_id=community.id, resolved=resolved)


class TestWhatItWillIterate:
    def test_a_list_of_uris(self, env):
        get_nodebb_replies_in_background(
            ['https://peer.test/p/1', 'https://peer.test/p/2'], env.community_id)
        assert env.resolved == ['https://peer.test/p/1', 'https://peer.test/p/2']

    def test_an_empty_list(self, env):
        get_nodebb_replies_in_background([], env.community_id)
        assert env.resolved == []

    @pytest.mark.parametrize('value', [None, 5, 0.5, True, {'a': 1}, set(),
                                       ('https://peer.test/p/1',)])
    def test_something_that_is_not_a_list(self, env, value):
        """D1340. None and a number raised; a dict iterated its keys; a tuple
        would have worked by accident. A peer's `orderedItems` is only usable as
        a list."""
        get_nodebb_replies_in_background(value, env.community_id)
        assert env.resolved == []

    def test_a_string_is_not_ten_single_letter_uris(self, env):
        """The measured one: `orderedItems: "https://peer.test/p/1"` sliced to a
        string, iterated to characters, and asked the peer for ten of them."""
        get_nodebb_replies_in_background('https://peer.test/p/1', env.community_id)
        assert env.resolved == []

    @pytest.mark.parametrize('entry', [5, None, [], {}, '', True])
    def test_an_entry_that_is_not_a_uri_is_skipped(self, env, entry):
        get_nodebb_replies_in_background(
            [entry, 'https://peer.test/p/good'], env.community_id)
        assert env.resolved == ['https://peer.test/p/good']

    def test_a_skipped_entry_does_not_use_up_the_allowance(self, env):
        """`reply_count` is what the cap counts, and an entry that names nothing
        should not spend it."""
        replies = [None] * 20 + [f'https://peer.test/p/{n}' for n in range(5)]
        get_nodebb_replies_in_background(replies, env.community_id)
        assert len(env.resolved) == 5


class TestHowManyItFetches:
    def test_at_most_ten(self, env):
        get_nodebb_replies_in_background(
            [f'https://peer.test/p/{n}' for n in range(50)], env.community_id)
        assert len(env.resolved) == 10

    def test_the_first_ten_in_order(self, env):
        get_nodebb_replies_in_background(
            [f'https://peer.test/p/{n}' for n in range(50)], env.community_id)
        assert env.resolved == [f'https://peer.test/p/{n}' for n in range(10)]

    def test_fewer_than_the_cap(self, env):
        get_nodebb_replies_in_background(
            ['https://peer.test/p/1'], env.community_id)
        assert len(env.resolved) == 1


class TestWhenOneReplyFails:
    def test_the_rest_are_still_fetched(self, env, monkeypatch):
        """D1340. The `except: raise` left the loop, so nine good replies were
        dropped for one bad one."""
        import app.activitypub.util as ap_util

        attempted = []

        def one_bad(uri, community, *args, **keywords):
            attempted.append(uri)
            if uri.endswith('/2'):
                raise ValueError('that one failed')

        monkeypatch.setattr(ap_util, 'resolve_remote_post', one_bad)
        get_nodebb_replies_in_background(
            [f'https://peer.test/p/{n}' for n in range(1, 5)], env.community_id)
        assert attempted == [f'https://peer.test/p/{n}' for n in range(1, 5)]

    def test_a_failing_reply_still_counts_towards_the_cap(self, env, monkeypatch):
        import app.activitypub.util as ap_util

        attempted = []

        def always_bad(uri, community, *args, **keywords):
            attempted.append(uri)
            raise ValueError('all of them failed')

        monkeypatch.setattr(ap_util, 'resolve_remote_post', always_bad)
        get_nodebb_replies_in_background(
            [f'https://peer.test/p/{n}' for n in range(50)], env.community_id)
        assert len(attempted) == 10

    def test_the_failure_is_logged(self, env, monkeypatch):
        import app.activitypub.util as ap_util

        logged = []
        monkeypatch.setattr(current_app.logger, 'info',
                            lambda message, *args: logged.append(message))
        monkeypatch.setattr(ap_util, 'resolve_remote_post',
                            lambda *args, **keywords: (_ for _ in ()).throw(
                                ValueError('boom')))
        get_nodebb_replies_in_background(['https://peer.test/p/1'],
                                         env.community_id)
        assert any('nodebb reply' in message for message in logged)


class TestTheCommunityItFetchesInto:
    def test_a_community_that_is_gone(self, env):
        get_nodebb_replies_in_background(['https://peer.test/p/1'], 999999)
        assert env.resolved == []

    def test_the_community_is_passed_on(self, env, monkeypatch):
        import app.activitypub.util as ap_util

        seen = []
        monkeypatch.setattr(ap_util, 'resolve_remote_post',
                            lambda uri, community, *args, **keywords: seen.append(community.id))
        get_nodebb_replies_in_background(['https://peer.test/p/1'], env.community_id)
        assert seen == [env.community_id]

    def test_nodebb_is_asked_for(self, env, monkeypatch):
        """`nodebb=True` is what tells `resolve_remote_post` to take the topic's
        audience when the post itself does not say where it belongs."""
        import app.activitypub.util as ap_util

        seen = []
        monkeypatch.setattr(ap_util, 'resolve_remote_post',
                            lambda uri, community, *args, **keywords: seen.append(keywords))
        get_nodebb_replies_in_background(['https://peer.test/p/1'], env.community_id)
        assert seen == [{'nodebb': True}]


class TestThePublishedTimeAPeerPutsOnAPost:
    """The other half of D1340, in `resolve_remote_post`'s tail.

    `object.posted_at = post_data['published']` put the peer's string straight into
    a DateTime column, and into `last_active`, which round 141 made NOT NULL. A
    `published` that is not a date was a DataError at commit -- which poisons the
    transaction, so the post that had just been created was lost along with the
    timestamp.
    """

    @pytest.fixture
    def resolvable(self, app, db_session, site, http_mock):
        from types import SimpleNamespace

        from tests.factories import (AS_PUBLIC_URI, PEER_OBJECT_HOST,
                                     PEER_OBJECT_URI, make_user, note_document,
                                     serve_remote_object)

        g.admin_ids = []
        # `seed_community_owner` creates the Instance and returns it; a second
        # `make_instance` for the same domain is a UniqueViolation on
        # ix_instance_domain.
        instance = seed_community_owner(PEER_OBJECT_HOST)
        author_uri = f'https://{PEER_OBJECT_HOST}/users/alice'
        author = make_user(instance, 'alice')
        author.ap_profile_id = author_uri
        author.ap_public_url = author_uri
        author.ap_fetched_at = db.func.now()
        author.ap_domain = PEER_OBJECT_HOST  # PERM-3: can_create_post checks the author's instance by it
        community = make_community('news', host=PEER_OBJECT_HOST)
        db.session.commit()
        return SimpleNamespace(
            community=community, author_uri=author_uri, http_mock=http_mock,
            uri=PEER_OBJECT_URI, public=AS_PUBLIC_URI,
            note=lambda **fields: note_document(
                attributed_to=author_uri, uri=PEER_OBJECT_URI,
                fields={'to': [AS_PUBLIC_URI], **fields}),
            serve=serve_remote_object)

    def resolve(self, resolvable, **fields):
        from app.activitypub.util import resolve_remote_post

        resolvable.serve(resolvable.http_mock, resolvable.uri,
                         resolvable.note(**fields))
        return resolve_remote_post(resolvable.uri, resolvable.community, None,
                                   False)

    def test_a_published_time_is_applied(self, resolvable):
        post = self.resolve(resolvable, published='2026-03-04T05:06:07Z')
        assert post is not None
        assert post.posted_at.year == 2026
        assert post.posted_at.month == 3

    @pytest.mark.parametrize('published', ['not a date', '', 5, [], {},
                                           '2026-13-45T99:99:99Z', None])
    def test_a_published_time_that_is_not_one_leaves_the_post_alone(
            self, resolvable, published):
        """The post is what matters: it arrives, with whatever timestamp the
        creation gave it, rather than being lost to a DataError."""
        post = self.resolve(resolvable, published=published)
        assert post is not None
        assert post.posted_at is not None

    def test_an_offset_is_converted(self, resolvable):
        """`parse_ap_timestamp` normalises to naive UTC, so a peer five hours
        ahead does not appear to have posted five hours from now."""
        post = self.resolve(resolvable, published='2026-03-04T12:00:00+05:00')
        assert post.posted_at.hour == 7

    def test_a_post_with_no_published_time(self, resolvable):
        post = self.resolve(resolvable)
        assert post is not None
        assert post.posted_at is not None
