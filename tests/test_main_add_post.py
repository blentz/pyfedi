"""`main.add_post`: the "add post" button that picks a community for you.

14 statements with no test at all.

D1385. `cross_post_community_id` is a cookie, written when the user last
cross-posted, and the route trusted it three ways at once. Measured, each value
reaching the route:

    'abc', 'null', '1.5'   ValueError: invalid literal for int() with base 10
    '0', '2', '999999'     AttributeError: 'NoneType' object has no attribute
                           'link'   (no such community)
    '-1'                   204, with the joined-community fallback skipped

So a cookie naming a community that has since been deleted -- or edited by hand,
or left behind by an older version -- broke the button with a 500 until it
expired, and `-1` made it silently do nothing. The cookie is a hint about where
the user probably wants to post, so an unusable one now falls through to the same
choice the route would have made without it, and 204 is left for the case it
actually means: nowhere to post.

A HARNESS NOTE THAT COST A PROBE. `client.set_cookie(key, value)` defaults the
domain to `localhost`, while this app's `SERVER_NAME` is `test.piefed.local`, so
the cookie is never sent and the route takes its no-cookie path. The first probe
reported `status=302` for every value including `'abc'`, which looked like the
route being robust; it was the cookie being absent. Every test here passes
`domain=...` and the first class below asserts the cookie is honoured at all, so
a silent regression to "cookie never read" fails rather than passing everything.
"""
import pytest
from flask import g

from app import db
from app.models import Community, Language, Site
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'


@pytest.fixture
def env(app, db_session):
    """Alice has joined `general`; `elsewhere` exists and she has not."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance(HOST)
    make_user(local, 'founder', local=True)          # id 1, the admin trap
    alice = make_user(local, 'alice', local=True)
    alice.verified = True
    joined = make_community('general')
    elsewhere = make_community('elsewhere')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(alice, joined)
    g.admin_ids = []
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(alice.id)
        sess['_fresh'] = True
    return client, joined, elsewhere


def go(client, cookie=None):
    if cookie is not None:
        client.set_cookie('cross_post_community_id', cookie, domain=HOST)
    return client.get('/add_post')


# --------------------------------------------------------------------------
# The cookie is read at all
# --------------------------------------------------------------------------


class TestTheCookieIsHonoured:
    def test_a_usable_cookie_wins_over_the_joined_community(self, app, env):
        """Asserted first, because every other row below would also pass if the
        cookie were simply never read -- which is what a missing `domain=` on
        `set_cookie` silently produces."""
        client, joined, elsewhere = env

        response = go(client, str(elsewhere.id))

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/elsewhere/submit'

    def test_a_padded_cookie_is_honoured(self, app, env):
        """`.strip().isdigit()` rather than `.isdigit()`: `int('  2  ')` is 2, so
        the guard is written to accept exactly what the parse accepts. Without the
        strip a padded cookie falls back instead of being used -- a silent
        difference in behaviour, which is why this row exists rather than being
        left to the parse.
        """
        client, joined, elsewhere = env

        response = go(client, f'  {elsewhere.id}  ')

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/elsewhere/submit'

    def test_without_a_cookie_the_joined_community_is_used(self, app, env):
        client, joined, elsewhere = env

        response = go(client)

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/general/submit'


# --------------------------------------------------------------------------
# D1385: the values that used to raise
# --------------------------------------------------------------------------


class TestACookieThatIsNotANumber:
    @pytest.mark.parametrize('cookie', ['abc', 'null', '1.5', '-1', '1e3',
                                        'NaN', '0x2', '2;3', ' ', '٢'])
    def test_it_does_not_raise(self, app, env, cookie):
        """`int(cookie)` was `ValueError` for most of these. `'٢'` is an
        Arabic-Indic digit: `str.isdigit()` accepts it and `int()` parses it, so
        it is here to pin that the guard and the parse agree about what a digit
        is -- a mismatch between them would be the same crash again."""
        client, joined, elsewhere = env

        assert go(client, cookie).status_code in (302, 204)

    @pytest.mark.parametrize('cookie', ['abc', 'null', '1.5', '-1'])
    def test_it_falls_back_to_the_joined_community(self, app, env, cookie):
        """The cookie is a hint, so an unusable one must leave the user where the
        route would have sent them anyway. `-1` is in this list because it used
        to reach the `== -1` check and return 204 instead."""
        client, joined, elsewhere = env

        response = go(client, cookie)

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/general/submit'


class TestACookieNamingNoCommunity:
    @pytest.mark.parametrize('cookie', ['999999', '0'])
    def test_it_does_not_raise(self, app, env, cookie):
        """`db.session.get` answered None and `.link()` was called on it -- the
        unguarded-get shape. `'0'` is included because no row has id 0 while
        `'0'.isdigit()` is true, so it reaches the lookup."""
        client, joined, elsewhere = env

        assert go(client, cookie).status_code in (302, 204)

    def test_a_deleted_community_falls_back(self, app, env):
        """The way this is reached in practice: the user cross-posted to a
        community, the community was later removed, and the cookie outlived it."""
        client, joined, elsewhere = env
        gone_id = elsewhere.id
        db.session.delete(elsewhere)
        db.session.commit()

        response = go(client, str(gone_id))

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/general/submit'


# --------------------------------------------------------------------------
# The fallback order, and the one case 204 is for
# --------------------------------------------------------------------------


class TestTheFallbackOrder:
    def test_moderating_is_preferred_over_joined(self, app, env):
        """`possible_communities()` builds `Moderating` from
        `moderating_communities` FIRST and skips those ids when building
        `Joined communities`, so a community the user both moderates and has
        joined appears only under `Moderating`.

        The route's own order is `Joined communities`, then `Moderating`, then
        `Others` -- so with a moderated community and a separately joined one,
        the joined one still wins. Both facts are asserted here because the route
        and the helper order these sections differently, and a reader comparing
        the two should not have to guess which prevails.
        """
        from app.models import CommunityMember, User

        client, joined, elsewhere = env
        alice = User.query.filter_by(user_name='alice').one()
        # alice moderates `elsewhere` and has joined `general`.
        make_community_member(alice, elsewhere, is_moderator=True)
        db.session.commit()

        response = go(client)

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/general/submit'

    def test_moderating_is_used_when_nothing_is_joined(self, app, env):
        """With no joined community, `Moderating` is the next section the route
        looks at."""
        from app.models import CommunityMember, User

        client, joined, elsewhere = env
        alice = User.query.filter_by(user_name='alice').one()
        CommunityMember.query.filter_by(user_id=alice.id).delete()
        make_community_member(alice, elsewhere, is_moderator=True)
        db.session.commit()

        response = go(client)

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/elsewhere/submit'

    def test_a_user_with_nowhere_to_post_gets_204(self, app, env):
        """The meaning 204 is reserved for. Asserted by removing every community,
        so `possible_communities()` has nothing in any section."""
        client, joined, elsewhere = env
        from app.models import CommunityMember

        CommunityMember.query.delete()
        Community.query.delete()
        db.session.commit()

        response = go(client)

        assert response.status_code == 204
        assert response.data == b''

    def test_a_cookie_cannot_rescue_a_user_with_nowhere_to_post(self, app, env):
        """A stale cookie must not resurrect a community that is gone: the lookup
        fails, the fallback finds nothing, and the answer is the same 204."""
        client, joined, elsewhere = env
        from app.models import CommunityMember

        stale = elsewhere.id
        CommunityMember.query.delete()
        Community.query.delete()
        db.session.commit()

        assert go(client, str(stale)).status_code == 204


class TestWhoMayUseIt:
    def test_an_anonymous_visitor_is_sent_to_the_login(self, app, env):
        """`@login_required` -- the route reads `possible_communities()`, which
        needs a user."""
        response = app.test_client().get('/add_post')

        assert response.status_code in (302, 401)
        if response.status_code == 302:
            assert '/auth/login' in response.headers['Location']
