"""D1411: a private instance's posts, readable through the embed and calendar routes.

The same sweep as D1410. `Site.private_instance` is enforced by the
`login_required_if_private_instance` decorator -- there is no `before_request` doing the
same job -- so a route is gated only where someone remembered to write it. D1410 fixed the
six RSS endpoints; the remaining GET routes carrying neither that decorator nor
`login_required` were read one at a time, and five of them serve a post.

Measured, anonymous, with `private_instance` on and one post titled SECRET TITLE. The
post's OWN page is gated, and these were not:

    /post/1/embed              200  title in body
    /post/1/embed_code         200  title in body
    /post/1/oembed             200  title in body
    /post/1                    302  -> /auth/login

`/post/<id>/ical` and `/community/<name>/ical` are the other two. Neither carried the
title in the probe -- the seeded post is not an event, so the calendar came back empty --
which is exactly why they are fixed on the argument rather than on the measurement: they
exist to serve an event's date, place and description, and nothing about them consulted
whether the instance is private.

THE DECORATOR, NOT THE UNCONDITIONAL `abort(404)` THE RSS ROUTES GOT. An RSS reader cannot
log in, so `index_rss` refuses everyone; a person following an embed link in a browser CAN
log in, and a member of a private instance should be able to see their own instance's
embeds and calendars. `login_required_if_private_instance` is the codebase's own idiom for
that, it already short-circuits ActivityPub requests, and using it keeps these five routes
behaving like `/post/<id>` itself rather than like the feeds.
"""
import pytest

from app import db
from app.models import Site
from tests.factories import make_community, make_instance, make_post, make_site, make_user

HOST = 'test.piefed.local'
SECRET = 'SECRET TITLE'


@pytest.fixture
def seeded(app, db_session):
    from types import SimpleNamespace

    make_site()
    site = db.session.get(Site, 1)
    instance = make_instance(HOST, software='piefed')
    author = make_user(instance, 'author', local=True)
    author.ap_profile_id = f'https://{HOST}/u/author'
    author.ap_public_url = f'https://{HOST}/u/author'
    author.ap_domain = HOST
    community = make_community('general')
    community.private = False
    post = make_post(community, author, f'https://{HOST}/p/1', title=SECRET)
    db.session.commit()
    return SimpleNamespace(site=site, community=community, author=author, post=post)


def _urls(seeded):
    p = seeded.post.id
    return {
        'the embed page': f'/post/{p}/embed',
        'the embed code': f'/post/{p}/embed_code',
        'the oembed document': f'/post/{p}/oembed',
        "a post's calendar": f'/post/{p}/ical',
        "a community's calendar": f'/community/{seeded.community.name}/ical',
    }


class TestAPrivateInstance:
    @pytest.mark.parametrize('name', list(_urls.__wrapped__ if hasattr(_urls, '__wrapped__')
                                          else ['the embed page', 'the embed code',
                                                'the oembed document',
                                                "a post's calendar",
                                                "a community's calendar"]))
    def test_an_anonymous_reader_is_sent_to_the_login_page(self, app, seeded, name):
        seeded.site.private_instance = True
        db.session.commit()

        response = app.test_client().get(_urls(seeded)[name])

        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']
        assert SECRET not in response.get_data(as_text=True)

    def test_that_is_what_the_posts_own_page_already_did(self, app, seeded):
        """The twin these five now match. A row asserting only the new behaviour would not
        say what the new behaviour was copied FROM."""
        seeded.site.private_instance = True
        db.session.commit()

        response = app.test_client().get(f'/post/{seeded.post.id}')

        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']

    def test_a_logged_in_member_still_gets_the_embed(self, app, seeded):
        """Why this is the decorator and not the RSS routes' unconditional refusal: a
        member of a private instance may read their own instance. If this row ever has to
        change, the choice between the two idioms is being revisited on purpose."""
        seeded.site.private_instance = True
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(seeded.author.id)
            session['_fresh'] = True

        response = client.get(f'/post/{seeded.post.id}/embed')

        assert response.status_code == 200
        assert SECRET in response.get_data(as_text=True)


class TestAPublicInstance:
    @pytest.mark.parametrize('name', ['the embed page', 'the embed code',
                                      'the oembed document'])
    def test_the_three_that_carried_the_title_still_serve_it(self, app, seeded, name):
        """The control. Without these rows a fix that refused everyone would pass every
        row above and break embeds for every public instance."""
        seeded.site.private_instance = False
        db.session.commit()

        response = app.test_client().get(_urls(seeded)[name])

        assert response.status_code == 200
        assert SECRET in response.get_data(as_text=True)

    @pytest.mark.parametrize('name', ["a post's calendar", "a community's calendar"])
    def test_the_two_calendars_are_reached_rather_than_refused(self, app, seeded, name):
        """The calendars are fixed on the argument, not on a leak that was measured: the
        seeded post is not an event, so `/post/<id>/ical` answers 404 and the community
        calendar answers an empty document. What these rows pin is that with privacy OFF
        the request reaches the route's own logic -- neither is a login redirect -- so the
        decorator is doing nothing on a public instance."""
        seeded.site.private_instance = False
        db.session.commit()

        response = app.test_client().get(_urls(seeded)[name])

        assert response.status_code in (200, 404)
        assert '/auth/login' not in response.headers.get('Location', '')


def test_the_five_routes_carry_the_decorator():
    """Read out of the source, because the decorator is the whole fix and a future edit
    that reorders decorators or drops one should fail here rather than in a probe.

    `@block_bots` sits above two of them and must stay above: it is about crawlers, not
    about privacy, and the order between them does not matter -- but the privacy decorator
    must be present on all five.
    """
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    expected = {('app/post/routes.py', 'post_embed'),
                ('app/post/routes.py', 'post_embed_code'),
                ('app/post/routes.py', 'post_oembed'),
                ('app/post/routes.py', 'show_post_ical'),
                ('app/community/routes.py', 'show_community_ical')}

    for path, name in expected:
        tree = ast.parse((root / path).read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        decorators = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}

        assert 'login_required_if_private_instance' in decorators, f'{path}:{name}'
