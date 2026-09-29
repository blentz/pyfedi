"""Round 246: what an anonymous viewer's block lists answer, and two fetch refusals.

`app/utils.py` has seven block/ban list helpers that each open with

    if user_id == 0:
        return []

`current_user.get_id()` answers `0` for a viewer with no account -- every caller passes it
straight through -- so that line is the anonymous path through all seven, and none of them had
a row for it. The guard is not an optimisation: without it the query runs with `user_id = 0`,
which matches no account and gives the same answer by accident rather than by decision, and
each of these lists is spread into SQL (`NOT IN :ids`) where an empty tuple is its own
problem.

Also here: `blocked_phrases`'s CRLF handling, which decides whether an admin editing the list
on Windows gets phrases that match anything; `blocked_referrers`; and the two refusals in
front of the OpenGraph parser and the thumbnail fetcher, both of which take a URL a peer
supplied and go and fetch it.
"""
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from flask import g

from app import db
from app.models import (CommunityBan, CommunityBlock, DomainBlock, Domain, InstanceBan,
                        InstanceBlock, Site, UserBlock)
from app.utils import (banned_instances, blocked_communities, blocked_domains,
                       blocked_instances, blocked_or_banned_instances, blocked_phrases,
                       blocked_referrers, blocked_users, communities_banned_from,
                       opengraph_parse, url_to_thumbnail_file)
from tests.factories import make_community, make_instance, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, site=g.site, baseline=api_baseline)


ANONYMOUS = 0

LIST_HELPERS = [communities_banned_from, blocked_domains, blocked_communities,
                blocked_or_banned_instances, blocked_instances, blocked_users,
                banned_instances]


# --------------------------------------------------------------------------
# The anonymous path through every block list
# --------------------------------------------------------------------------


class TestWhatAnAnonymousViewerBlocks:
    """Seven helpers, one guard each, all reached with the id `0` that
    `current_user.get_id()` gives a viewer with no account.
    """

    @pytest.fixture
    def seeded(self, env):
        """Rows in every one of the seven tables, so an answer of `[]` is a claim about the
        guard rather than about an empty database."""
        viewer = make_user(env.baseline.instance_local, 'blocker', local=True)
        target = make_user(env.baseline.instance_local, 'blocked', local=True)
        community = make_community('blockedland')
        peer = make_instance('blocked.example')
        domain = Domain(name='blocked.example', post_count=0)
        db.session.add(domain)
        db.session.commit()
        db.session.add_all([
            CommunityBan(user_id=viewer.id, community_id=community.id),
            CommunityBlock(user_id=viewer.id, community_id=community.id),
            DomainBlock(user_id=viewer.id, domain_id=domain.id),
            InstanceBlock(user_id=viewer.id, instance_id=peer.id),
            InstanceBan(user_id=viewer.id, instance_id=peer.id),
            UserBlock(blocker_id=viewer.id, blocked_id=target.id),
        ])
        db.session.commit()
        env.viewer = viewer
        env.target = target
        env.community = community
        env.peer = peer
        env.domain = domain
        return env

    @pytest.mark.parametrize('helper', LIST_HELPERS,
                             ids=[helper.__name__ for helper in LIST_HELPERS])
    def test_an_anonymous_viewer_has_nothing_blocked(self, seeded, helper):
        """The answer, for all seven, with rows in every one of the tables behind them.

        Deleting any ONE of these guards is an EQUIVALENT MUTANT: `user_id` is a serial
        primary key, so no account has id 0 and the query it skips matches nothing anyway.
        What the guard buys is the skipped query -- seven of them on every anonymous request
        -- and a statement of intent at the top of each helper. The mutants that are NOT
        equivalent, and are killed by the control row below, are the ones that make the
        guard answer for every viewer.
        """
        assert helper(ANONYMOUS) == []

    def test_a_signed_in_viewer_gets_their_own_lists(self, seeded):
        """The control for all seven. Without it the rows above would pass against helpers
        that always answer `[]`, which is the failure mode that matters: a block list that
        is silently empty is a block that does not apply."""
        viewer_id = seeded.viewer.id

        assert communities_banned_from(viewer_id) == [seeded.community.id]
        assert blocked_communities(viewer_id) == [seeded.community.id]
        assert blocked_domains(viewer_id) == [seeded.domain.id]
        assert blocked_instances(viewer_id) == [seeded.peer.id]
        assert banned_instances(viewer_id) == [seeded.peer.id]
        assert blocked_users(viewer_id) == [seeded.target.id]

    def test_blocked_or_banned_instances_is_the_union_of_the_two(self, seeded):
        """It concatenates the blocks a viewer made with the bans a peer made against them,
        which is why it is a separate helper -- and why its anonymous guard has to be its
        own line rather than inherited from the two it calls."""
        assert sorted(blocked_or_banned_instances(seeded.viewer.id)) == \
            [seeded.peer.id, seeded.peer.id]

    def test_a_community_ban_on_another_instance_counts_too(self, seeded):
        """`communities_banned_from` is a UNION: community bans plus every community on an
        instance that has banned this viewer. The second half is what makes an
        instance-level ban hide a community the viewer was never banned from directly."""
        theirs = make_community('theirland', host='blocked.example')
        theirs.instance_id = seeded.peer.id
        db.session.commit()

        assert theirs.id in communities_banned_from(seeded.viewer.id)


# --------------------------------------------------------------------------
# The admin's two text lists
# --------------------------------------------------------------------------


class TestThePhrasesAnAdminBlocks:
    """`blocked_phrases` is checked against every incoming post's title and body, so a
    phrase that silently fails to match is a filter that does nothing.
    """

    def _phrases(self, env, value):
        env.site.blocked_phrases = value
        db.session.commit()
        return blocked_phrases()

    def test_each_line_is_a_phrase(self, env):
        assert self._phrases(env, 'buy now\nfree money') == ['buy now', 'free money']

    def test_a_windows_line_ending_is_trimmed(self, env):
        """`if phrase.endswith('\\r')`. The admin box is a textarea, and a browser on
        Windows submits CRLF -- so without this every phrase but the last carries a trailing
        `\\r` and matches nothing, which is a blocklist that quietly stops working."""
        assert self._phrases(env, 'buy now\r\nfree money\r\n') == ['buy now',
                                                                  'free money']

    def test_blank_lines_are_skipped(self, env):
        """`if phrase != ''`. An empty phrase would be `'' in post.title`, which is True for
        every post -- so a stray blank line would block everything."""
        assert self._phrases(env, 'buy now\n\n\nfree money') == ['buy now', 'free money']

    def test_an_unset_list_is_empty(self, env):
        """The `else`. `None.split` is an `AttributeError` on every incoming post."""
        assert self._phrases(env, None) == []
        assert self._phrases(env, '') == []


class TestTheReferrersAnAdminDeclines:

    def _referrers(self, env, value):
        env.site.auto_decline_referrers = value
        db.session.commit()
        return blocked_referrers()

    def test_each_line_is_a_referrer(self, env):
        assert self._referrers(env, 'spam.example\nscam.example') == ['spam.example',
                                                                     'scam.example']

    def test_blank_lines_are_skipped(self, env):
        assert self._referrers(env, 'spam.example\n\n') == ['spam.example']

    def test_an_unset_list_is_empty(self, env):
        assert self._referrers(env, None) == []


# --------------------------------------------------------------------------
# Two refusals in front of an outbound fetch
# --------------------------------------------------------------------------


class TestReadingAPagesOpengraph:
    """`opengraph_parse` fetches a URL a peer put in a post and reads its meta tags. It
    answers None for anything it cannot read, because every caller writes `if opengraph:`.
    """

    def test_an_empty_url_is_refused_without_fetching(self, env):
        with patch('app.utils.parse_page') as parse:
            assert opengraph_parse('') is None

        assert parse.call_count == 0

    def test_a_query_string_is_dropped_before_fetching(self, env):
        """The URL is truncated at the `?`. That is a deliberate narrowing -- a tracking
        query would otherwise be sent to the third party this instance is fetching from --
        and it is asserted by the URL `parse_page` actually receives."""
        fetched = []
        with patch('app.utils.parse_page',
                   side_effect=lambda url: fetched.append(url) or {'og:title': 'x'}):
            opengraph_parse('https://news.example/story?utm_source=piefed&id=7')

        assert fetched == ['https://news.example/story']

    def test_a_failure_answers_none(self, env):
        """The bare `except Exception`. `parse_page` reaches somebody else's server, so
        anything can come back -- and a traceback here would be a 500 on a page that was
        merely trying to show a thumbnail."""
        with patch('app.utils.parse_page', side_effect=Exception('unreachable')):
            assert opengraph_parse('https://news.example/story') is None

    def test_the_parsed_tags_are_returned_unchanged(self, env):
        """The control: the function must pass the answer through, or the two rows above
        would hold for one that always returned None."""
        with patch('app.utils.parse_page', return_value={'og:image': 'https://x/y.png'}):
            assert opengraph_parse('https://news.example/story') == {
                'og:image': 'https://x/y.png'}


class TestFetchingARemoteThumbnail:
    """`url_to_thumbnail_file` downloads an image a peer named. Two refusals come before
    anything is written: a URI this instance will not request at all, and a request that
    fails.
    """

    def test_a_uri_this_instance_refuses_is_not_fetched(self, env):
        """`is_invalid_get_request_uri` is the SSRF guard -- it refuses `.local` hosts and
        private addresses among others -- and it runs before the request, so the row asserts
        nothing was fetched rather than that nothing was stored."""
        with patch('app.utils.httpx_client') as client:
            assert url_to_thumbnail_file('https://thing.local/image.png') is None

        assert client.get.call_count == 0

    def test_a_failed_request_answers_none(self, env):
        """The bare `except:`. The host is a peer's choice, so a refused connection is
        ordinary and must not raise out of whatever was rendering the post."""
        with patch('app.utils.httpx_client') as client:
            client.get.side_effect = httpx.ConnectError('no route to host')

            assert url_to_thumbnail_file('https://news.example/image.png') is None

    def test_the_slow_host_gets_a_longer_timeout(self, env):
        """A hardcoded exception for `washingtonpost.com`, whose thumbnails take longer than
        the five seconds everything else gets. Asserted because it is invisible otherwise,
        and because a timeout is how this instance limits what one peer's link can cost
        it."""
        timeouts = []

        def record(url, timeout=None):
            timeouts.append(timeout)
            raise httpx.ConnectError('stop here')

        with patch('app.utils.httpx_client') as client:
            client.get.side_effect = record
            url_to_thumbnail_file('https://www.washingtonpost.com/i.png')
            url_to_thumbnail_file('https://news.example/i.png')

        assert timeouts == [15, 5]
