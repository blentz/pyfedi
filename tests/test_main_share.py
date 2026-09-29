"""`/share`: the public endpoint a browser's share sheet or a bookmarklet posts to.

D1386, the third reader of `cross_post_community_id` and the last unguarded one:

    app/post/routes.py:2680   guarded already -- catches (TypeError, ValueError)
                              and checks the row resolves, with a comment naming
                              both failures
    app/main/routes.py:286    guarded as D1385 last round
    app/main/routes.py:949    int(request.cookies.get(...)) -- this one

`/share` carries no `@login_required`, so anything can follow it, and a
non-numeric cookie was a 500. Measured:

    cookie='abc'      ValueError: invalid literal for int() with base 10: 'abc'
    cookie='null'     ValueError
    cookie='1.5'      ValueError
    cookie='0'        200   (only the form field is set here, so there is no
    cookie='999999'   200    `.link()` to fail on -- unlike add_post)

Pre-selecting the community the user last cross-posted to is a convenience, so an
unusable cookie leaves the field empty rather than breaking the page.

THIS ROUND EXISTS BECAUSE THE PREVIOUS ONE DID NOT SWEEP. D1385 fixed one reader
of this cookie and did not grep for the others; one of the two it missed had
already been fixed independently, and the remaining one was on the public route.
Fact 839.
"""
import itertools

import pytest
from flask import g

from app import db
from app.models import Community, Language, Site
from tests.factories import (make_community, make_community_member,
                             make_instance, make_post, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'
URL = 'https://example.com/an-article'


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance(HOST)
    make_user(local, 'founder', local=True)
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
    return client, joined, elsewhere, alice


def share(client, cookie=None, **params):
    if cookie is not None:
        client.set_cookie('cross_post_community_id', cookie, domain=HOST)
    params.setdefault('url', URL)
    return client.get('/share', query_string=params)


# --------------------------------------------------------------------------
# D1386
# --------------------------------------------------------------------------


class TestTheRememberedCommunityCookie:
    @staticmethod
    def _selected(response) -> bytes:
        """The `<option selected ...>` markup, which is the only witness that the
        cookie was used.

        `value="2"` appears in the dropdown whether or not it is the selection --
        every joinable community is an option -- so asserting on it is asserting
        on the chrome, and three mutants survived that: "the cookie is never
        read", "the digit test loses its strip" and "no community is ever
        preselected". Measured: no cookie gives `selected count=0`, a usable one
        gives `<option selected value="2">`.
        """
        import re

        found = re.findall(rb'<option selected value="(\d+)"', response.data)
        return found[0] if found else b''

    def test_a_usable_cookie_preselects_that_community(self, app, env):
        """Asserted first: every row below would also pass if the cookie were
        never read, which is what a `set_cookie` without `domain=` produces
        (fact 837)."""
        client, joined, elsewhere, alice = env

        response = share(client, str(elsewhere.id))

        assert response.status_code == 200
        assert self._selected(response) == str(elsewhere.id).encode()

    def test_without_a_cookie_nothing_is_preselected(self, app, env):
        client, joined, elsewhere, alice = env

        assert self._selected(share(client)) == b''

    @pytest.mark.parametrize('cookie', ['abc', 'null', '1.5', '-1', 'NaN',
                                        '0x2', ' ', '2;3'])
    def test_a_cookie_that_is_not_a_number_does_not_raise(self, app, env, cookie):
        client, joined, elsewhere, alice = env

        response = share(client, cookie)

        assert response.status_code == 200
        assert self._selected(response) == b'', 'an unusable cookie selected one'

    @pytest.mark.parametrize('cookie', ['999999', '0'])
    def test_a_cookie_naming_no_community_does_not_raise(self, app, env, cookie):
        """This site only sets a form field, so there was no `.link()` to fail
        on -- but the row is still looked up, and a missing one must not be
        pre-selected."""
        client, joined, elsewhere, alice = env

        response = share(client, cookie)

        assert response.status_code == 200
        assert self._selected(response) == b''

    def test_a_deleted_community_is_not_preselected(self, app, env):
        client, joined, elsewhere, alice = env
        gone_id = elsewhere.id
        db.session.delete(elsewhere)
        db.session.commit()

        response = share(client, str(gone_id))

        assert response.status_code == 200
        assert self._selected(response) == b''

    def test_a_padded_cookie_is_honoured(self, app, env):
        """`.strip().isdigit()`, matching what `int()` accepts (fact 838)."""
        client, joined, elsewhere, alice = env

        response = share(client, f'  {elsewhere.id}  ')

        assert response.status_code == 200
        assert self._selected(response) == str(elsewhere.id).encode()


# --------------------------------------------------------------------------
# The rest of the route
# --------------------------------------------------------------------------


class TestTheUrlParameter:
    def test_a_request_with_no_url_is_a_400(self, app, env):
        """Already fixed before this round -- the route's own comment records
        that a missing `url` used to be `AttributeError` on `.strip()`. Pinned
        because nothing else asserted it."""
        client, joined, elsewhere, alice = env

        assert client.get('/share').status_code == 400

    def test_an_empty_url_is_a_400(self, app, env):
        client, joined, elsewhere, alice = env

        assert client.get('/share', query_string={'url': ''}).status_code == 400

    def test_a_whitespace_only_url_is_served(self, app, env):
        """Current behaviour, pinned rather than changed: `if not url` sees a
        truthy '   ', and `remove_tracking_from_link('')` leaves an empty string,
        so the page renders and its "already posted" query looks for `Post.url ==
        ''`. Harmless, and out of this round's scope -- but worth recording so the
        next reader knows the 400 covers an ABSENT url, not a blank one."""
        client, joined, elsewhere, alice = env

        assert client.get('/share',
                          query_string={'url': '   '}).status_code == 200

    def test_a_youtu_be_link_is_canonicalised_before_the_lookup(self, app, env):
        """`remove_tracking_from_link` rewrites **youtu.be** links to their
        youtube.com form and drops the query with them -- it does NOT strip
        `utm_*` from arbitrary URLs, which is what this test assumed first. It is
        checked here through the "already shared" lookup, because the url is never
        echoed into the page: sharing the short link must match a post someone
        made with the canonical one.
        """
        client, joined, elsewhere, alice = env
        # Measured, not assumed: the function produces `https://youtube.com/...`
        # with NO `www.`, and the lookup is an exact string match on `Post.url` --
        # so a post stored with the `www.` form would not match a shared youtu.be
        # link. That is the product's behaviour today, and this test uses the form
        # the function actually emits rather than the one a browser would show.
        canonical = 'https://youtube.com/watch?v=dQw4w9WgXcQ'
        a_post_with_url(joined, alice, url=canonical, title='YOUTUBEPOST')

        response = share(client, url='https://youtu.be/dQw4w9WgXcQ?si=trackingid')

        assert response.status_code == 200
        assert PHRASE in response.data

    def test_the_page_renders_its_form(self, app, env):
        """The url itself is not echoed into share.html -- it travels in the
        redirect this form submits to -- so the witness is the form, not the url.
        Checked rather than assumed: asserting the url appeared failed here."""
        client, joined, elsewhere, alice = env

        response = share(client)

        assert b'which_community' in response.data


PHRASE = b'already been shared'


_post_counter = itertools.count(1)


def a_post_with_url(community, user, url=URL, **columns):
    """`make_post`'s third positional is `ap_id`, NOT `url` -- it has no `url`
    parameter at all -- so the column this route matches on has to be set here.
    The first version of these tests relied on it and matched nothing, which the
    `already been shared` phrase never appearing would have shown if the
    assertions had not had an `or` escape in them.

    The `ap_id` is counted rather than derived from the url: several tests need two
    posts sharing one url, and deriving it collided on `ap_id`'s unique index.
    """
    post = make_post(community, user,
                     f'https://test.piefed.local/p/{next(_post_counter)}',
                     **columns)
    post.url = url
    db.session.commit()
    return post


class TestCommunitiesThatAlreadyHaveTheLink:
    """The witness is the page's own phrase, not a post title: share.html renders
    `<a href="...">{{ community.display_name() }}</a>`, never the title."""

    def test_a_community_with_the_same_url_is_listed(self, app, env):
        client, joined, elsewhere, alice = env
        a_post_with_url(joined, alice, title='ALREADYPOSTED')

        response = share(client)

        assert PHRASE in response.data
        assert b'general' in response.data

    def test_a_deleted_post_does_not_count(self, app, env):
        """`Post.deleted == False` -- a link whose only post was removed has not
        been posted."""
        client, joined, elsewhere, alice = env
        a_post_with_url(joined, alice, title='REMOVEDPOST').deleted = True
        db.session.commit()

        assert PHRASE not in share(client).data

    def test_a_bots_post_does_not_count(self, app, env):
        """`Post.from_bot == False`."""
        client, joined, elsewhere, alice = env
        a_post_with_url(joined, alice, title='BOTPOST').from_bot = True
        db.session.commit()

        assert PHRASE not in share(client).data

    def test_a_microblog_post_does_not_500_the_page(self, app, env):
        """D1387. `share.html` does `posts_keyed_by_community[community.id]`, and
        the two queries behind it disagreed: the post query excluded
        `Post.microblog == False` while the community query only excluded the
        community NAMED 'microblogs'. A microblog post -- what a Mastodon Note
        with no title becomes, in any community -- listed its community with no
        entry in the dict. Measured: `UndefinedError: dict object has no element
        1`, a 500 on a public route.
        """
        client, joined, elsewhere, alice = env
        a_post_with_url(joined, alice, title='MICROBLOGPOST', microblog=True)

        response = share(client)

        assert response.status_code == 200
        assert PHRASE not in response.data

    def test_a_microblog_post_beside_an_ordinary_one_still_lists_it(self, app, env):
        """The other direction: aligning the queries must not lose a community
        that has a real post as well as a microblog one."""
        client, joined, elsewhere, alice = env
        a_post_with_url(joined, alice, title='MICROBLOGPOST', microblog=True)
        a_post_with_url(elsewhere, alice, title='ORDINARYPOST')

        response = share(client)

        assert response.status_code == 200
        assert PHRASE in response.data
        assert b'elsewhere' in response.data


class TestWhoMayUseIt:
    def test_an_anonymous_visitor_is_served(self, app, env):
        """No `@login_required`: the route is what a browser's share sheet hits,
        and it has to render before the visitor signs in. That is also why the
        cookie crash mattered -- it was reachable without an account."""
        response = app.test_client().get('/share', query_string={'url': URL})

        assert response.status_code in (200, 302)

    def test_an_anonymous_visitor_with_a_bad_cookie_is_served(self, app, env):
        client = app.test_client()
        client.set_cookie('cross_post_community_id', 'abc', domain=HOST)

        response = client.get('/share', query_string={'url': URL})

        assert response.status_code in (200, 302)
