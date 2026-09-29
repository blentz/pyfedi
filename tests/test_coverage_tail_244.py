"""Round 244: what this instance fetches, who it recommends, and who it restricts.

Four clusters in `app/utils.py`, none of which had rows.

    retrieve_block_list          fetches a domain blocklist from a GitHub raw URL. An admin
    retrieve_peertube_block_list pastes the answer into the ban list, so what these return
                                 on a failure decides whether a fetch error empties it.
    jaccard_similarity           the post recommender: how alike two accounts' upvotes are,
                                 with a cache and a minimum-activity floor.
    libretranslate_string        an outbound translation call, cached for a day.
    user_in_restricted_country   whether an account is in a country the admin has
                                 restricted NSFW content for.

The two blocklist fetches matter most: both reach a third party, and a bare `except:`
deciding between `None` and `''` is the difference between "leave the list alone" and "the
list is empty now".
"""
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
from flask import g

from app import cache, db
from app.models import Site, User
from app.utils import (jaccard_similarity, libretranslate_string, retrieve_block_list,
                       retrieve_peertube_block_list, user_in_restricted_country,
                       user2_cache)
from tests.factories import (make_community, make_community_member, make_post,
                             make_post_reply, make_post_reply_vote, make_post_vote,
                             make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(app=app, site=g.site, baseline=api_baseline)


BLOCK_LIST_URL = ('https://raw.githubusercontent.com/rimu/no-qanon/master/'
                  'domains.txt')
PEERTUBE_URL = ('https://peertube_isolation.frama.io/list/'
                'peertube_isolation.json')


# --------------------------------------------------------------------------
# The two blocklists this instance downloads
# --------------------------------------------------------------------------


class TestDownloadingTheDomainBlockList:
    """An admin fetches this list and pastes it into the site's banned domains. The
    function returns the raw text, or None when the fetch fails -- and the caller can tell
    those apart, which is what stops a network error from being read as an empty list.
    """

    def test_the_body_is_returned_when_the_fetch_succeeds(self, env, http_mock):
        http_mock.get(BLOCK_LIST_URL).respond(200, text='bad.example\nworse.example\n')

        assert retrieve_block_list() == 'bad.example\nworse.example\n'

    def test_a_failed_request_answers_none_rather_than_an_empty_list(self, env,
                                                                    http_mock):
        """The bare `except:`. A network failure must not look like a list with nothing in
        it, because the difference decides whether an admin's ban list is replaced with
        nothing."""
        http_mock.get(BLOCK_LIST_URL).mock(
            side_effect=httpx.ConnectError('no route to host'))

        assert retrieve_block_list() is None

    def test_a_non_200_answers_nothing_at_all(self, env, http_mock):
        """`if response and response.status_code == 200` -- with no `else`, so the function
        falls off the end and returns None for a 404 or a 500. The same answer as a
        refused connection, which is the conservative one."""
        http_mock.get(BLOCK_LIST_URL).respond(404, text='not found')

        assert retrieve_block_list() is None


class TestDownloadingThePeertubeBlockList:
    """The same idea in JSON: a list of `{'value': domain}` rows, joined into one
    newline-separated string.
    """

    def test_the_values_are_joined_into_one_list(self, env, http_mock):
        http_mock.get(PEERTUBE_URL).respond(
            200, json={'data': [{'value': 'one.example'},
                                {'value': 'two.example'}]})

        assert retrieve_peertube_block_list() == 'one.example\ntwo.example'

    def test_the_trailing_newline_is_stripped(self, env, http_mock):
        """`list.strip()`. Each row appends its own newline, so without the strip the last
        entry is followed by a blank line -- and a blank line in a ban list is a domain
        nobody typed."""
        http_mock.get(PEERTUBE_URL).respond(200, json={'data': [{'value': 'one.example'}]})

        assert retrieve_peertube_block_list() == 'one.example'

    def test_a_failed_request_answers_none(self, env, http_mock):
        http_mock.get(PEERTUBE_URL).mock(
            side_effect=httpx.ConnectError('no route to host'))

        assert retrieve_peertube_block_list() is None

    def test_a_non_200_answers_an_empty_string(self, env, http_mock):
        """The asymmetry between the two functions, pinned rather than smoothed over: this
        one initialises `list = ''` BEFORE the status check, so a 404 returns `''` where
        `retrieve_block_list` returns None. A caller that treats them the same is wrong
        about one of them."""
        http_mock.get(PEERTUBE_URL).respond(500, json={})

        assert retrieve_peertube_block_list() == ''


# --------------------------------------------------------------------------
# How alike two accounts are
# --------------------------------------------------------------------------


class TestHowAlikeTwoAccountsAre:
    """`jaccard_similarity` powers "people who liked this also liked". It compares two sets
    of upvoted things -- posts and replies, keyed `post/<id>` and `reply/<id>` -- and
    answers a percentage, or zero for an account that has not upvoted enough to judge.
    """

    @pytest.fixture
    def seeded(self, env):
        user2_cache.clear()
        community = make_community('similarland')
        author = make_user(env.baseline.instance_local, 'similarauthor', local=True)
        other = make_user(env.baseline.instance_local, 'theother', local=True)
        db.session.commit()
        make_community_member(author, community)
        posts = [make_post(community, author,
                           ap_id=f'https://test.piefed.local/s/{n}', title=f'post {n}')
                 for n in range(16)]
        db.session.commit()
        env.community = community
        env.author = author
        env.other = other
        env.posts = posts
        yield env
        user2_cache.clear()

    def _upvote(self, env, user, posts):
        for post in posts:
            make_post_vote(user, post, 1.0)
        db.session.commit()

    def test_two_accounts_that_upvoted_the_same_things_are_a_hundred_percent(self,
                                                                            seeded):
        """Identical sets: the intersection and the union are the same size."""
        self._upvote(seeded, seeded.other, seeded.posts[:13])
        mine = {f'post/{post.id}' for post in seeded.posts[:13]}

        assert jaccard_similarity(mine, seeded.other.id) == 100

    def test_a_half_overlap_is_a_third(self, seeded):
        """Jaccard is intersection over UNION, not over either set -- ten shared out of
        sixteen between them is 10/16, not 10/13. The row uses numbers where the two
        answers differ."""
        self._upvote(seeded, seeded.other, seeded.posts[:13])
        mine = {f'post/{post.id}' for post in seeded.posts[3:16]}

        assert jaccard_similarity(mine, seeded.other.id) == pytest.approx(62.5)

    def test_an_account_with_too_few_upvotes_scores_zero(self, seeded):
        """`if len(user2_upvoted) > 12`. Twelve upvotes is not enough to say anything about
        somebody's taste, and a small set makes a high similarity trivially easy -- two
        accounts sharing one upvote would otherwise read as 100%."""
        self._upvote(seeded, seeded.other, seeded.posts[:12])
        mine = {f'post/{post.id}' for post in seeded.posts[:12]}

        assert jaccard_similarity(mine, seeded.other.id) == 0

    def test_replies_count_as_well_as_posts(self, seeded):
        """The two id lists are prefixed `post/` and `reply/` so they cannot collide -- a
        post and a reply that happen to share an id are different things."""
        reply_ids = []
        for n in range(13):
            reply = make_post_reply(seeded.posts[0], seeded.author, body=f'reply {n}')
            db.session.commit()
            make_post_reply_vote(seeded.other, reply, 1.0)
            reply_ids.append(reply.id)
        db.session.commit()
        mine = {f'reply/{reply_id}' for reply_id in reply_ids}

        assert jaccard_similarity(mine, seeded.other.id) == 100

    def test_the_second_accounts_upvotes_are_cached(self, seeded):
        """`user2_cache` is a plain module-level dict, not flask-caching, so it survives
        for the life of the process. The row proves the cache is READ by changing the
        database behind it."""
        self._upvote(seeded, seeded.other, seeded.posts[:13])
        mine = {f'post/{post.id}' for post in seeded.posts[:13]}
        first = jaccard_similarity(mine, seeded.other.id)

        self._upvote(seeded, seeded.other, seeded.posts[13:16])

        assert jaccard_similarity(mine, seeded.other.id) == first
        user2_cache.clear()
        assert jaccard_similarity(mine, seeded.other.id) != first


# --------------------------------------------------------------------------
# Translation, and the country restriction
# --------------------------------------------------------------------------


class TestTranslatingAString:

    @pytest.fixture
    def real_cache(self, app, monkeypatch):
        from cachelib import SimpleCache

        monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
        return cache

    def test_the_translated_text_is_returned(self, env, real_cache):
        with patch('app.utils.LibreTranslateAPI') as api:
            api.return_value.translate.return_value = 'bonjour'

            assert libretranslate_string('hello', 'en', 'fr') == 'bonjour'

    def test_a_failure_is_logged_and_answered_with_an_empty_string(self, env,
                                                                   real_cache):
        """The endpoint is somebody else's server. An exception here would otherwise
        propagate into whatever page asked for the translation, so the answer is '' and the
        traceback goes to the log."""
        with patch('app.utils.LibreTranslateAPI',
                   side_effect=Exception('the endpoint is down')):
            assert libretranslate_string('hello', 'en', 'fr') == ''

    def test_the_answer_is_cached_per_string_and_language_pair(self, env, real_cache):
        """`@cache.memoize`. A translation is an outbound request per string, so the
        memoize key has to include the languages -- otherwise asking for French returns
        the German answer."""
        with patch('app.utils.LibreTranslateAPI') as api:
            api.return_value.translate.side_effect = ['bonjour', 'hallo']

            first = libretranslate_string('hello', 'en', 'fr')
            again = libretranslate_string('hello', 'en', 'fr')
            german = libretranslate_string('hello', 'en', 'de')

        assert first == 'bonjour'
        assert again == 'bonjour'
        assert german == 'hallo'


class TestTheNsfwCountryRestriction:
    """An admin can list country codes where NSFW content is not shown. The check reads the
    account's geolocated country, which may be unset.
    """

    def _user(self, env, country):
        user = make_user(env.baseline.instance_local, f'from{country or "nowhere"}',
                         local=True)
        user.ip_address_country = country
        db.session.commit()
        return user

    def _restrict(self, env, value):
        """`Settings.value` holds JSON, and `get_setting` returns the default when it
        cannot be parsed -- so a newline-separated list has to be written with `json.dumps`,
        not wrapped in quotes by hand. A raw newline inside a JSON string is invalid, and
        the symptom is a restriction that silently matches nobody.
        """
        import json

        from app.models import Settings

        existing = Settings.query.filter_by(name='nsfw_country_restriction').first()
        if existing:
            existing.value = json.dumps(value)
        else:
            db.session.add(Settings(name='nsfw_country_restriction',
                                    value=json.dumps(value)))
        db.session.commit()

    def test_an_account_in_a_restricted_country_is_restricted(self, env):
        self._restrict(env, 'GB\nFR')

        assert user_in_restricted_country(self._user(env, 'GB')) is True

    def test_an_account_elsewhere_is_not(self, env):
        self._restrict(env, 'GB\nFR')

        assert user_in_restricted_country(self._user(env, 'DE')) is False

    def test_surrounding_whitespace_in_the_setting_is_ignored(self, env):
        """`country_code.strip()`. The setting is a textarea, so a trailing space on a line
        is ordinary -- and without the strip that country is never matched."""
        self._restrict(env, ' GB \n FR ')

        assert user_in_restricted_country(self._user(env, 'GB')) is True

    def test_an_account_with_no_country_is_not_restricted(self, env):
        """`user.ip_address_country and ...`. The column is NULL for anybody whose address
        was never geolocated, and `None in [...]` is False anyway -- but the guard is what
        makes the function return a falsy value rather than None."""
        self._restrict(env, 'GB')

        assert not user_in_restricted_country(self._user(env, None))
