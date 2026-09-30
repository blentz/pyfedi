"""Round 267: the cache key a page is stored under, and the other small decisions in app/utils.py.

The weightiest cluster is `make_cache_key`, which is what `@cache.cached()` stores a RENDERED PAGE
under. Two arms:

    anonymous   the url, the sort, the post id, the literal `anon`, and the `Accept` and
                `Accept-Language` headers
    signed in   the url, the sort, the post id, the literal `user`, and the ACCOUNT ID

Every element is load-bearing in the same direction: two requests that must see different pages have
to produce different keys, or one reader is served another reader's page out of the cache. The
`Accept` header is in there because the same url answers both HTML and ActivityPub.

The rest are one- and two-line arms: the mixed http/https rewrite an instance like
retro.piefed.com serves its web UI with, `get_setting`'s unreadable-JSON fallback, the two
`httpx.HTTPError` normalisations every caller of `get_request` relies on, `is_local_image_url`'s host
allowlist, the tag filter on a listing, a recipient's notification language, and the two arms an
instance that has been unreachable for five days ends at.
"""
import json as jsonlib
from unittest.mock import patch

import httpx
import pytest
from flask import g

from app import db
from app.models import Instance, Language, Post, Settings, Site, User, utcnow
from app.utils import (awaken_dormant_instance, get_recipient_language, get_request, get_setting,
                       instance_sticky_post_ids, is_local_image_url, make_cache_key)
from tests.factories import make_community, make_instance, make_post, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('utilsland')
    author = make_user(api_baseline.instance_local, 'utilsauthor', local=True)
    db.session.commit()
    from types import SimpleNamespace
    return SimpleNamespace(app=app, community=community, author=author, baseline=api_baseline)


# --------------------------------------------------------------------------
# The key a rendered page is cached under
# --------------------------------------------------------------------------


class TestTheKeyARenderedPageIsCachedUnder:
    """`make_cache_key` is passed to `@cache.cached()`, so its answer decides WHOSE page a later
    request is served. Every row here is one element of the key, asserted by making two requests
    that must not share a page and checking the keys differ.
    """

    def _key(self, env, path, **kwargs):
        with env.app.test_request_context(path, **kwargs):
            return make_cache_key()

    def test_an_anonymous_key_says_anon_and_names_no_account(self, env):
        key = self._key(env, '/')

        assert '_anon_' in key
        assert '_user_' not in key

    def test_a_signed_in_key_names_the_account(self, env):
        """`_user_{current_user.id}`. Without the id every signed-in reader shares one cache entry,
        and the first one's page -- their subscriptions, their filters, their vote arrows -- is
        served to the rest."""
        with env.app.test_request_context('/'):
            with patch('app.utils.current_user') as reader:
                reader.is_anonymous = False
                reader.id = env.author.id
                key = make_cache_key()

        assert f'_user_{env.author.id}' in key
        assert '_anon_' not in key

    def test_two_accounts_do_not_share_a_key(self, env):
        other = make_user(env.baseline.instance_local, 'otherreader', local=True)
        db.session.commit()
        keys = []

        for account in (env.author, other):
            with env.app.test_request_context('/'):
                with patch('app.utils.current_user') as reader:
                    reader.is_anonymous = False
                    reader.id = account.id
                    keys.append(make_cache_key())

        assert keys[0] != keys[1]

    def test_an_anonymous_visitor_does_not_share_a_signed_in_key(self, env):
        anonymous = self._key(env, '/')
        with env.app.test_request_context('/'):
            with patch('app.utils.current_user') as reader:
                reader.is_anonymous = False
                reader.id = env.author.id
                signed_in = make_cache_key()

        assert anonymous != signed_in

    def test_the_accept_header_is_part_of_an_anonymous_key(self, env):
        """The comment on that line says why: the SAME url answers HTML to a browser and
        ActivityPub JSON to a peer, and both are anonymous. One key for both serves a peer's JSON
        to a reader, or a page of HTML to a server expecting an actor document."""
        html = self._key(env, '/', headers={'Accept': 'text/html'})
        activitypub = self._key(env, '/',
                                headers={'Accept': 'application/activity+json'})

        assert html != activitypub

    def test_the_accept_language_header_is_part_of_it_too(self, env):
        """Anonymous pages are rendered in the language the browser asked for, so the key has to
        carry it or the first visitor's language is served to everybody."""
        english = self._key(env, '/', headers={'Accept-Language': 'en'})
        german = self._key(env, '/', headers={'Accept-Language': 'de'})

        assert english != german

    def test_the_url_is_part_of_the_key(self, env):
        assert self._key(env, '/') != self._key(env, '/communities')

    def test_the_query_string_is_part_of_the_key(self, env):
        """`request.url`, not `request.path` -- so `?page=2` is a different page, which it is."""
        assert self._key(env, '/?page=1') != self._key(env, '/?page=2')

    def test_the_query_string_is_part_of_a_signed_in_key_too(self, env):
        """`request.url` on BOTH branches. The two keys are separate f-strings, so `page=2` being
        in one says nothing about the other -- and a signed-in reader paging through a listing is
        the commoner case of the two."""
        keys = []

        for query in ('?page=1', '?page=2'):
            with env.app.test_request_context(f'/{query}'):
                with patch('app.utils.current_user') as reader:
                    reader.is_anonymous = False
                    reader.id = env.author.id
                    keys.append(make_cache_key())

        assert keys[0] != keys[1]

    def test_the_sort_and_the_post_id_are_part_of_the_key(self, env):
        with env.app.test_request_context('/'):
            assert make_cache_key(sort='hot') != make_cache_key(sort='new')
            assert make_cache_key(post_id=1) != make_cache_key(post_id=2)


# --------------------------------------------------------------------------
# A page served over http while federation uses https
# --------------------------------------------------------------------------


class TestTheMixedProtocolRewrite:
    """`if current_app.config['HTTP_PROTOCOL'] == 'mixed'`. The comment names the instance it is
    for: a web UI served over http while federation happens over https. Every url the templates
    rendered says https, so the rendered page is rewritten on the way out -- and only for this
    instance's OWN server name, because a peer's https url must stay https.
    """

    def test_this_instances_own_urls_are_rewritten(self, env, monkeypatch):
        from app.utils import render_template

        monkeypatch.setitem(env.app.config, 'HTTP_PROTOCOL', 'mixed')
        server_name = env.app.config['SERVER_NAME']

        with env.app.test_request_context('/'):
            with patch('app.utils.flask.render_template',
                       return_value=f'<a href="https://{server_name}/c/x">ours</a>'
                                    f'<a href="https://peer.example/c/y">theirs</a>'):
                response = render_template('anything.html')

        body = response.get_data(as_text=True)
        assert f'http://{server_name}/c/x' in body
        assert 'https://peer.example/c/y' in body

    def test_nothing_is_rewritten_in_the_ordinary_configuration(self, env, monkeypatch):
        from app.utils import render_template

        monkeypatch.setitem(env.app.config, 'HTTP_PROTOCOL', 'https')
        server_name = env.app.config['SERVER_NAME']

        with env.app.test_request_context('/'):
            with patch('app.utils.flask.render_template',
                       return_value=f'<a href="https://{server_name}/c/x">ours</a>'):
                response = render_template('anything.html')

        assert f'https://{server_name}/c/x' in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# A setting that cannot be read
# --------------------------------------------------------------------------


class TestASettingThatCannotBeRead:

    def test_an_unreadable_setting_falls_back_to_the_default(self, env):
        """`except JSONDecodeError: return default`. The column holds JSON written by the admin
        forms, and a row that predates a format change -- or one edited by hand -- would otherwise
        raise out of whatever page read it. Every caller passes a default because every caller has
        a sane one."""
        db.session.add(Settings(name='a_broken_setting', value='not json at all'))
        db.session.commit()

        assert get_setting('a_broken_setting', 'the default') == 'the default'

    def test_a_readable_setting_is_returned(self, env):
        db.session.add(Settings(name='a_good_setting', value=jsonlib.dumps(False)))
        db.session.commit()

        assert get_setting('a_good_setting', True) is False

    def test_a_missing_setting_falls_back_too(self, env):
        assert get_setting('no_such_setting_at_all', 42) == 42


# --------------------------------------------------------------------------
# Every transport failure becomes one class
# --------------------------------------------------------------------------


class TestEveryFetchFailureBecomesAnHttpError:
    """`get_request`'s contract, which the refresh tasks' retries are written against: whatever
    httpx raises, the caller sees `httpx.HTTPError`. Each arm is one httpx class that is NOT an
    HTTPError, so an unconverted one escapes past every handler in every caller.
    """

    def test_a_value_error_is_converted(self, env):
        with patch('app.utils.httpx_client.get',
                   side_effect=ValueError('not a url httpx will accept')):
            with pytest.raises(httpx.HTTPError, match='not a url httpx will accept'):
                get_request('https://peer.example/thing')

    def test_a_stream_error_is_converted(self, env):
        """`httpx.StreamError` descends from `RuntimeError`, not from `HTTPError`."""
        with patch('app.utils.httpx_client.get',
                   side_effect=httpx.StreamError('the stream went away')):
            with pytest.raises(httpx.HTTPError, match='the stream went away'):
                get_request('https://peer.example/thing')

    def test_an_invalid_url_is_converted(self, env):
        """`httpx.InvalidURL` descends straight from Exception, which is the case the comment above
        that handler records."""
        with patch('app.utils.httpx_client.get',
                   side_effect=httpx.InvalidURL('that is not a url')):
            with pytest.raises(httpx.HTTPError, match='that is not a url'):
                get_request('https://peer.example/thing')


class TestWhichImageUrlsAreOurs:
    """`is_local_image_url` decides whether an image is served by this instance, which is what the
    callers use to avoid re-fetching and re-storing their own files.

    `is_image_url` below it asks the SERVER first (`mime_type_using_head`) and only falls back to
    the extension, so every row here answers that question locally rather than registering a HEAD
    per url -- what is under test is the HOST allowlist, not the content-type sniff.
    """

    @pytest.fixture(autouse=True)
    def no_head_requests(self):
        with patch('app.utils.mime_type_using_head', return_value=None):
            yield

    def test_an_image_on_this_server_is_local(self, env):
        server_name = env.app.config['SERVER_NAME']

        assert is_local_image_url(f'https://{server_name}/static/media/x.png') is True

    def test_an_image_on_localhost_is_local(self, env):
        """`127.0.0.1` is in the list because that is what a development instance serves from."""
        assert is_local_image_url('http://127.0.0.1/static/media/x.png') is True

    def test_an_image_in_our_own_bucket_is_local(self, env, monkeypatch):
        monkeypatch.setitem(env.app.config, 'S3_PUBLIC_URL', 'cdn.example')

        assert is_local_image_url('https://cdn.example/posts/ab/cd/x.png') is True

    def test_a_peers_image_is_not_local(self, env):
        assert is_local_image_url('https://peer.example/media/x.png') is False

    def test_something_that_is_not_an_image_is_not_local_either(self, env):
        """The first guard. The answer is about images, so a page on this very server is still
        False -- a caller treating it as a local image would try to resize an HTML document."""
        server_name = env.app.config['SERVER_NAME']

        assert is_local_image_url(f'https://{server_name}/c/utilsland') is False


# --------------------------------------------------------------------------
# An instance that stopped answering
# --------------------------------------------------------------------------


class TestAnInstanceThatStoppedAnswering:
    """`awaken_dormant_instance` is called when something is delivered to a dormant instance. The
    arm here is the give-up: after about five days of failures it is marked `gone_forever`, and
    nothing is queued for it again.
    """

    def test_an_instance_that_has_failed_for_five_days_is_given_up_on(self, env):
        instance = make_instance('dormant.example')
        instance.dormant = True
        instance.gone_forever = False
        instance.start_trying_again = utcnow() + __import__('datetime').timedelta(days=6)
        db.session.commit()

        awaken_dormant_instance(instance)
        db.session.commit()

        db.session.expire_all()
        refreshed = db.session.get(Instance, instance.id)
        assert refreshed.gone_forever is True
        assert refreshed.dormant is True

    def test_an_instance_still_within_the_window_is_not_given_up_on(self, env):
        """The comparison is `utcnow() + 5 days < start_trying_again`, so a retry scheduled for
        tomorrow is one this instance is still trying."""
        instance = make_instance('sleepy.example')
        instance.dormant = True
        instance.gone_forever = False
        instance.start_trying_again = utcnow() + __import__('datetime').timedelta(days=1)
        db.session.commit()

        awaken_dormant_instance(instance)
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Instance, instance.id).gone_forever is False

    def test_an_instance_with_no_retry_time_is_left_alone(self, env):
        """`if instance.start_trying_again and ...`. None is not a date to compare, and a dormant
        instance with no scheduled retry has not been failing for five days -- it has not been
        tried yet."""
        instance = make_instance('untried.example')
        instance.dormant = True
        instance.gone_forever = False
        instance.start_trying_again = None
        db.session.commit()

        awaken_dormant_instance(instance)
        db.session.commit()

        db.session.expire_all()
        assert db.session.get(Instance, instance.id).gone_forever is False

    def test_nothing_happens_for_an_instance_that_is_not_dormant(self, env):
        assert awaken_dormant_instance(None) is None


# --------------------------------------------------------------------------
# Which language a notification is written in
# --------------------------------------------------------------------------


class TestWhichLanguageANotificationIsWrittenIn:
    """`get_recipient_language` exists because a notification is rendered in the RECIPIENT's
    language rather than in the language of whoever triggered it. Three arms, in order of what the
    recipient actually chose.
    """

    def test_the_posting_language_is_used_when_it_is_set(self, env):
        language = Language(code='de', name='German')
        db.session.add(language)
        db.session.commit()
        env.author.language_id = language.id
        env.author.interface_language = 'fr'
        db.session.commit()

        assert get_recipient_language(env.author.id) == 'de'

    def test_the_interface_language_is_the_fallback(self, env):
        env.author.language_id = None
        env.author.interface_language = 'fr'
        db.session.commit()

        assert get_recipient_language(env.author.id) == 'fr'

    def test_english_is_the_last_resort(self, env):
        env.author.language_id = None
        env.author.interface_language = None
        db.session.commit()

        assert get_recipient_language(env.author.id) == 'en'


class TestTheInstanceWidePinnedPosts:

    def test_only_pinned_undeleted_posts_are_listed(self, env):
        """`instance_sticky_post_ids` is read on every render of the home page, which is why it is
        memoized -- and the three conditions in its SQL are each one a post could fail."""
        pinned = make_post(env.community, env.author, ap_id='https://test.piefed.local/s/1')
        pinned.instance_sticky = True
        deleted_pin = make_post(env.community, env.author,
                                ap_id='https://test.piefed.local/s/2')
        deleted_pin.instance_sticky = True
        deleted_pin.deleted = True
        ordinary = make_post(env.community, env.author, ap_id='https://test.piefed.local/s/3')
        db.session.commit()

        from app import cache
        cache.delete_memoized(instance_sticky_post_ids)
        ids = instance_sticky_post_ids()

        assert pinned.id in ids
        assert deleted_pin.id not in ids
        assert ordinary.id not in ids
