"""Round 230: the public endpoints in `app/main/routes.py` that anybody can drive.

Four surfaces, all reachable without an account, and none of them had a row on its
consequential line:

    /honey/<whatever>       the scraper trap. Three visits inside 24 hours write
                            `ban:{ip}` for FOUR WEEKS, and `block_honey_pot()` turns that
                            key into a 403 on unrelated pages. Only the counting was
                            covered; the ban itself, and the 403 it causes, were not.
    /bot_challenge/<uuid>   clears or confirms an account's bot flag, from a URL alone.
    /webhook                accepts JSON from anyone and fires a plugin hook with it.
    /share                  a public form that writes a 28-day cookie.

The honeypot rows are the ones that matter most. A ban is written from a request the
banned party does not make -- three visits to a trap URL -- and it is read on every page
by `block_honey_pot`, so getting either half wrong is either a scraper that walks the
site freely or a real reader locked out for four weeks. Both halves are asserted here,
end to end, rather than by checking that a redis key exists.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import BotChallenge, Site
from tests.factories import make_community, make_community_member, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    g.site.honeypot = True
    user = make_user(api_baseline.instance_local, 'trapwatcher', local=True)
    user.verified = True
    user.private_key = 'x'
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return SimpleNamespace(app=app, client=client, anonymous=app.test_client(),
                           user=user, baseline=api_baseline, site=g.site)


@pytest.fixture
def redis():
    from app import redis_client

    assert redis_client is not None, 'these rows are about what is written to redis'
    for pattern in ('honeypot:*', 'ban:*'):
        keys = redis_client.keys(pattern)
        if keys:
            redis_client.delete(*keys)
    yield redis_client
    for pattern in ('honeypot:*', 'ban:*'):
        keys = redis_client.keys(pattern)
        if keys:
            redis_client.delete(*keys)


# --------------------------------------------------------------------------
# /honey: the trap, the ban it writes, and the 403 that ban causes
# --------------------------------------------------------------------------


class TestTheScraperTrap:

    def test_a_third_visit_bans_the_address_for_four_weeks(self, env, redis):
        """`:769`. Two visits are not a ban and the third is, so both sides of `count >= 3`
        are asserted in one row -- a threshold tested only from above would be satisfied by
        banning on the first visit.

        The expiry is checked because it is the whole risk in this endpoint: the value is
        four weeks, and a mistake in the arithmetic is invisible to any assertion that only
        asks whether the key exists.
        """
        env.anonymous.get('/honey/one')
        env.anonymous.get('/honey/two')

        # The key is read back rather than rebuilt from `ip_address()`: that helper answers
        # from the live request, and a test client's address is not something this row
        # should have to know to be about the threshold.
        assert redis.keys('ban:*') == []

        env.anonymous.get('/honey/three')

        banned = redis.keys('ban:*')
        assert len(banned) == 1
        assert 86400 * 7 * 4 - 60 < redis.ttl(banned[0]) <= 86400 * 7 * 4

    def test_the_ban_is_what_refuses_an_ordinary_page(self, env, redis):
        """The other half, and the reason the ban is worth writing: `block_honey_pot()`
        runs at the top of the community, feed, domain and inbox views, so the key written
        by `/honey` is read on pages that have nothing to do with it."""
        community = make_community('victimland')
        db.session.commit()

        before = env.anonymous.get(f'/c/{community.name}')
        for n in range(3):
            env.anonymous.get(f'/honey/{n}')
        after = env.anonymous.get(f'/c/{community.name}')

        assert before.status_code == 200
        assert after.status_code == 403

    def test_a_signed_in_visitor_is_never_trapped(self, env, redis):
        """The first arm. A logged-in account following a hidden link is a person with a
        rss reader or a prefetcher, not a scraper, so the trap records nothing at all --
        and a row that only counted visits would not notice this arm disappearing."""
        for n in range(4):
            env.client.get(f'/honey/{n}')

        assert redis.keys('honeypot:*') == []
        assert redis.keys('ban:*') == []

    @pytest.mark.parametrize('headers', [
        {'Sec-Fetch-Dest': 'image'},
        {'Sec-Fetch-Dest': 'audio'},
        {'Sec-Fetch-Dest': 'video'},
        {'Accept': 'image/avif,image/webp,*/*'},
    ])
    def test_a_media_fetch_is_not_treated_as_a_visit(self, env, redis, headers):
        """The second arm. A browser that prefetches or renders the trap URL as an image
        sends these, and banning on them would ban ordinary readers -- which is the
        expensive direction of this endpoint's two possible mistakes."""
        for n in range(4):
            env.anonymous.get(f'/honey/{n}', headers=headers)

        assert redis.keys('ban:*') == []

    def test_the_trap_still_answers_with_something(self, env, redis):
        """It returns 100 characters of gibberish rather than an error: a trap that
        announced itself with a 403 would be skipped by the next crawler."""
        response = env.anonymous.get('/honey/whatever')

        assert response.status_code == 200
        assert len(response.get_data(as_text=True)) > 0

    def test_the_honeypot_can_be_switched_off_by_the_site(self, env, redis):
        """`block_honey_pot` reads `g.site.honeypot`, so an instance that has turned the
        feature off must not enforce a ban that is still in redis -- the key outlives the
        setting by up to four weeks."""
        community = make_community('victimland')
        db.session.commit()
        for n in range(3):
            env.anonymous.get(f'/honey/{n}')
        env.site.honeypot = False
        db.session.commit()

        response = env.anonymous.get(f'/c/{community.name}')

        assert response.status_code == 200


# --------------------------------------------------------------------------
# /bot_challenge/<uuid>
# --------------------------------------------------------------------------


class TestTheBotChallenge:
    """A URL that clears an account's bot flag. There is no login on it -- the `uuid` IS
    the credential -- so the rows below pin both which uuid works and what an unknown one
    does.
    """

    def test_answering_the_challenge_clears_the_flag(self, env):
        """`:1580-1582`. The flag is read back from the database rather than from the page,
        because the page is the same generic template either way."""
        challenge = BotChallenge(uuid='11111111-1111-1111-1111-111111111111',
                                 user_id=env.user.id, is_a_bot=None)
        db.session.add(challenge)
        db.session.commit()

        response = env.anonymous.get(f'/bot_challenge/{challenge.uuid}')

        assert response.status_code == 200
        assert 'human' in response.get_data(as_text=True)
        assert challenge.is_a_bot is False

    def test_a_challenge_already_marked_a_bot_is_not_cleared(self, env):
        """`:1576-1577`. `is_a_bot is True` means the deadline passed, and following the
        link afterwards must NOT undo that -- otherwise the flag is worth nothing to
        whoever set it."""
        challenge = BotChallenge(uuid='22222222-2222-2222-2222-222222222222',
                                 user_id=env.user.id, is_a_bot=True)
        db.session.add(challenge)
        db.session.commit()

        response = env.anonymous.get(f'/bot_challenge/{challenge.uuid}')

        assert response.status_code == 200
        assert 'flagged as a bot' in response.get_data(as_text=True)
        assert challenge.is_a_bot is True

    def test_an_unknown_uuid_is_a_404(self, env):
        """The `else`. `is_a_bot is True` is an identity test, so `None` -- waiting for an
        answer -- takes the clearing arm; only a uuid nobody holds gets nothing."""
        response = env.anonymous.get('/bot_challenge/33333333-3333-3333-3333-333333333333')

        assert response.status_code == 404


# --------------------------------------------------------------------------
# /webhook
# --------------------------------------------------------------------------


class TestTheWebhook:
    """R223, fixed. `/webhook` took JSON from anyone, with no signature, secret or token,
    and handed it to `plugins.fire_hook("webhook", payload)`. It now answers only a caller
    presenting `WEBHOOK_SECRET` in `X-Webhook-Secret`, and 404s while no secret is
    configured (owner ruling 2026-09-30).
    """

    SECRET = 'a-shared-secret'

    @pytest.fixture
    def fired(self, env, monkeypatch):
        fired = []
        monkeypatch.setattr('app.main.routes.plugins.fire_hook',
                            lambda name, payload: fired.append((name, payload)))
        monkeypatch.setitem(env.app.config, 'WEBHOOK_SECRET', self.SECRET)
        return fired

    def test_a_payload_is_accepted_and_handed_to_the_plugins(self, env, fired):
        """The hook call is intercepted so the row asserts WHAT was passed on, not merely
        that the request was accepted."""
        response = env.anonymous.post('/webhook', json={'event': 'ping', 'id': 7},
                                      headers={'X-Webhook-Secret': self.SECRET})

        assert response.status_code == 202
        assert fired == [('webhook', {'event': 'ping', 'id': 7})]

    def test_with_no_secret_configured_the_endpoint_does_not_exist(self, env, fired,
                                                                    monkeypatch):
        monkeypatch.setitem(env.app.config, 'WEBHOOK_SECRET', '')

        response = env.anonymous.post('/webhook', json={'event': 'ping'},
                                      headers={'X-Webhook-Secret': ''})

        assert response.status_code == 404
        assert fired == []

    @pytest.mark.parametrize('headers', [{}, {'X-Webhook-Secret': 'wrong'}],
                             ids=['missing', 'wrong'])
    def test_a_caller_without_the_secret_is_refused(self, env, fired, headers):
        response = env.anonymous.post('/webhook', json={'event': 'ping'}, headers=headers)

        assert response.status_code == 403
        assert fired == []

    def test_an_empty_payload_is_refused_without_firing_anything(self, env, fired):
        """An empty JSON object is falsy, so `{}` is refused by the same branch that
        refuses a missing body -- and nothing reaches the plugins."""
        response = env.anonymous.post('/webhook', json={},
                                      headers={'X-Webhook-Secret': self.SECRET})

        assert response.status_code == 400
        assert response.get_json()['error'] == 'no payload received'
        assert fired == []

    def test_a_body_that_is_not_json_is_refused_too(self, env, fired):
        """`request.get_json()` raises on an unparseable body unless it is allowed to
        fail, so this row says which of the two happens today: a 415 from Flask rather than
        the endpoint's own 400. Either way no hook fires."""
        response = env.anonymous.post('/webhook', data='not json',
                                      content_type='text/plain',
                                      headers={'X-Webhook-Secret': self.SECRET})

        assert response.status_code >= 400
        assert fired == []


# --------------------------------------------------------------------------
# /share and /my-year-in-review
# --------------------------------------------------------------------------


class TestSharingALink:

    def test_a_submitted_share_redirects_and_remembers_the_community(self, env):
        """`:960-967`. The cookie is what makes the next share default to the same
        community, and the three deletions clear a half-finished post from an earlier
        attempt -- so a row that only checked the redirect would not notice them."""
        community = make_community('sharehere')
        make_community_member(env.user, community)
        db.session.commit()

        response = env.client.post('/share?url=https://example.com/story&title=Story',
                                   data={'which_community': community.id},
                                   follow_redirects=False)

        assert response.status_code == 302
        # `community.add_post` builds `/community/<name>/submit/<type>`.
        assert response.headers['Location'].startswith(
            f'/community/{community.name}/submit/link')
        assert 'link=https://example.com/story' in response.headers['Location']
        cookies = response.headers.getlist('Set-Cookie')
        assert any(f'cross_post_community_id={community.id}' in c for c in cookies)
        for cleared in ['post_title', 'post_description', 'post_tags']:
            assert any(c.startswith(f'{cleared}=;') for c in cookies), cleared

    def test_an_id_outside_the_form_choices_never_reaches_the_lookup(self, env):
        """`:960`'s `or abort(404)` is not reachable, and this row says why rather than
        leaving the arm looking untested.

        `form.which_community.choices` comes from `possible_communities()`, which offers
        every community that is not banned, not private-without-membership, and not on a
        dead instance -- so a community the viewer has never joined IS a valid choice, and
        a BANNED one is not. WTForms' `SelectField` refuses anything outside its choices
        before `validate_on_submit()` returns True, so the request below never reaches the
        lookup and the page is re-rendered instead.

        That leaves `db.session.get(...) or abort(404)` reachable only if a community
        disappears between the choices being built and the same request reading it back,
        which is a window inside one function call -- so the arm is recorded as unreachable
        rather than left looking untested.
        """
        mine = make_community('sharehere')
        make_community_member(env.user, mine)
        db.session.commit()

        response = env.client.post(
            '/share?url=https://example.com/story',
            data={'which_community': env.baseline.banned_community.id})

        assert response.status_code == 200
        assert 'Set-Cookie' not in response.headers or \
            not any('cross_post_community_id' in c
                    for c in response.headers.getlist('Set-Cookie'))


class TestTheYearInReview:

    def test_it_says_there_is_nothing_to_show(self, env):
        """`:1591`. A route that exists to answer a URL other software links to, and whose
        whole content is that this instance does not keep the data."""
        response = env.client.get('/my-year-in-review/2026')

        assert response.status_code == 200
        # The apostrophe is HTML-escaped in the rendered page.
        assert 'track you' in response.get_data(as_text=True)

    def test_it_needs_a_login(self, env):
        response = env.anonymous.get('/my-year-in-review/2026')

        assert response.status_code == 302
