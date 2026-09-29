"""D1412: the last of the sweep -- what a private instance still told an anonymous visitor.

D1410 closed six RSS endpoints and D1411 five embed and calendar routes. Its commit message
recorded four more that answered 200 and were left for a round with its own argument,
because none of them carried a post's title and "leaks content" was the wrong description.
Measured, anonymous, with `private_instance` on:

    /post/1/options_menu                 200  the post's ap_id            988 bytes
    /post/1/markdown_source/options_menu 200  the post's ap_id            988 bytes
    /post/1/share_mastodon               200  nothing identifying      12,934 bytes
    /user/1/preview                      200  the user's BIO and name   1,146 bytes
    /u/author/feeds                      200  the user's name          12,163 bytes
    /u/author/myfeeds                    302  -> /auth/login
    /feeds                               302  -> /auth/login

THE ARGUMENT, now that the measurement is in front of it. `/user/<id>/preview` renders
`about_html` -- a user's own prose, written for the members of a private instance and served
to anyone who tried a small integer. That is content, and it settles the round: these are
gated. The options menus disclose a post's `ap_id`, which is where the post lives on its home
instance; an anonymous visitor learning that a post with id 1 exists here and is
`https://.../p/1` there is exactly what "only members may read this instance" is meant to
prevent. `/u/<actor>/feeds` and `/post/<id>/share_mastodon` disclose less -- a name that was
in the url already, and a share form -- and are gated with the others rather than left as the
only ungated members of their own blueprints, which is how this class of defect keeps
reappearing.

`login_required_if_private_instance`, as D1411 used: all five are pages a person opens in a
browser, and a member of a private instance should be able to open them.

`/u/<actor>/myfeeds` and `/feeds` needed nothing: they already carry `@login_required`, which
is stricter -- a public instance requires a session for them too. They are in the table below
as the twins these five now resemble.
"""
import pytest

from app import db
from app.models import Site
from tests.factories import make_community, make_instance, make_post, make_site, make_user

HOST = 'test.piefed.local'
SECRET = 'SECRET TITLE'
BIO = 'MY PRIVATE BIO'


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
    author.about = BIO
    author.about_html = f'<p>{BIO}</p>'
    community = make_community('general')
    community.private = False
    post = make_post(community, author, f'https://{HOST}/p/1', title=SECRET)
    db.session.commit()
    return SimpleNamespace(site=site, community=community, author=author, post=post)


def _urls(seeded):
    p, uid = seeded.post.id, seeded.author.id
    return {
        "a post's options menu": f'/post/{p}/options_menu',
        'the same menu with markdown': f'/post/{p}/markdown_source/options_menu',
        'the mastodon share form': f'/post/{p}/share_mastodon',
        "a user's preview card": f'/user/{uid}/preview',
        "a user's feed list": f'/u/{seeded.author.user_name}/feeds',
    }

NAMES = ["a post's options menu", 'the same menu with markdown',
         'the mastodon share form', "a user's preview card", "a user's feed list"]


class TestAPrivateInstance:
    @pytest.mark.parametrize('name', NAMES)
    def test_an_anonymous_visitor_is_sent_to_the_login_page(self, app, seeded, name):
        seeded.site.private_instance = True
        db.session.commit()

        response = app.test_client().get(_urls(seeded)[name])

        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']

    def test_the_preview_card_no_longer_serves_a_members_bio(self, app, seeded):
        """The row that settled the round. `about_html` is a user's own prose, written for
        the members of a private instance; before this it answered to anyone who tried a
        small integer."""
        seeded.site.private_instance = True
        db.session.commit()

        body = app.test_client().get(f'/user/{seeded.author.id}/preview').get_data(
            as_text=True)

        assert BIO not in body

    def test_the_options_menu_no_longer_discloses_where_the_post_lives(self, app, seeded):
        """The other measured disclosure: `ap_id` is the post's address on its home
        instance, so serving it tells an anonymous visitor both that the post exists here
        and where to read it there."""
        seeded.site.private_instance = True
        db.session.commit()

        body = app.test_client().get(
            f'/post/{seeded.post.id}/options_menu').get_data(as_text=True)

        assert seeded.post.ap_id not in body

    def test_a_logged_in_member_still_gets_all_five(self, app, seeded):
        """Why this is the decorator and not the RSS routes' unconditional refusal."""
        seeded.site.private_instance = True
        db.session.commit()
        client = app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(seeded.author.id)
            session['_fresh'] = True

        for name in NAMES:
            assert client.get(_urls(seeded)[name]).status_code == 200, name


class TestAPublicInstance:
    @pytest.mark.parametrize('name', NAMES)
    def test_every_one_of_them_still_answers(self, app, seeded, name):
        """The control. Without it a fix that refused everyone would pass every row above
        and break five working pages on every public instance."""
        seeded.site.private_instance = False
        db.session.commit()

        assert app.test_client().get(_urls(seeded)[name]).status_code == 200

    def test_the_preview_card_still_shows_the_bio(self, app, seeded):
        seeded.site.private_instance = False
        db.session.commit()

        body = app.test_client().get(f'/user/{seeded.author.id}/preview').get_data(
            as_text=True)

        assert BIO in body


def test_the_two_that_needed_nothing_are_stricter_still():
    """`/u/<actor>/myfeeds` and `/feeds` carry `@login_required`, so they require a session
    on a PUBLIC instance too -- stricter than this round's rule, and the reason they were
    already 302 in the probe. Asserted so that a later edit swapping the stronger decorator
    for the weaker one shows up as a loss."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    for path, name in [('app/user/routes.py', 'user_myfeeds'),
                       ('app/feed/routes.py', 'feeds')]:
        tree = ast.parse((root / path).read_text())
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == name), None)
        if fn is None:
            continue
        decorators = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}

        assert 'login_required' in decorators, f'{path}:{name}'


def test_the_five_routes_carry_the_decorator():
    """Read out of the source, as D1411's equivalent row does."""
    import ast
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    expected = {('app/post/routes.py', 'post_options'),
                ('app/post/routes.py', 'post_reply_options'),
                ('app/post/routes.py', 'post_share_mastodon'),
                ('app/user/routes.py', 'user_preview'),
                ('app/user/routes.py', 'user_feeds')}

    for path, name in expected:
        tree = ast.parse((root / path).read_text())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        decorators = {d.id for d in fn.decorator_list if isinstance(d, ast.Name)}

        assert 'login_required_if_private_instance' in decorators, f'{path}:{name}'
