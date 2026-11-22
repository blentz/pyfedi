# ruff: noqa: F811 -- the fixtures imported below are named again as test parameters, which is how pytest uses them
"""The sidebar's live feed box (spec 2026-11-22-sidebar-live-feed): an optional box, hidden until shown, offered to
every user; signed-in readers see the newest posts of the home page's All tab, or of its Subscribed tab when they
chose followed people and communities, kept current by static/js/live_feed.js."""
import re
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations

from app import db
from app.community.live import SIDEBAR_LIVE_LIMIT, sidebar_live_filter, sidebar_live_hidden
from app.models import User
from app.sidebar import COMMUNITY, HOME, domain_offered, layout, shows_box
from app.utils import utcnow
from tests.conftest import forget_login
from tests.factories import make_community, make_follow, make_post, make_user
from tests.test_branding import load_migration
from tests.test_branding_pages import page
from tests.test_home_live import home, login  # noqa: F401 -- fixture
from tests.test_user_sidebar import press, reader, reader_row, set_reader, set_site, settings_page, signed_in  # noqa: F401
from tests.two_domains import default_request

URL = '/sidebar/live_posts'   # the `home` world starts with two recent posts: a test of exact ids starts after them

pytestmark = pytest.mark.usefixtures('site')


def item_ids(html):
    return [int(found) for found in re.findall(r'id="sidebar_live_(\d+)"', html)]


# ---------------------------------------------------------------------------------------------------------------------
# An optional box

def test_it_is_hidden_until_shown():
    assert dict(layout('', HOME))['live_feed'] is False
    assert dict(layout('about,new_communities', COMMUNITY))['live_feed'] is False
    assert dict(layout('live_feed', HOME))['live_feed'] is True


def test_it_is_offered_where_the_domain_hides_it(app, branded):
    set_site(home_sidebar='-about,-live_feed')
    with default_request(app):
        offered = dict(domain_offered(HOME))
    assert offered['live_feed'] is False and 'about' not in offered


def test_a_domain_that_shows_it_shows_it_to_everyone_who_has_not_chosen(app, branded, reader):
    set_site(home_sidebar='live_feed')
    assert 'id="live_feed_card"' in page(app, '/', user=reader)
    set_reader(reader, home_sidebar='-live_feed')
    assert 'id="live_feed_card"' not in page(app, '/', user=reader)


def test_nobody_sees_it_by_default(app, branded, reader):
    assert 'id="live_feed_card"' not in page(app, '/', user=reader)
    assert 'id="live_feed_card"' not in page(app, '/c/goldart', user=reader)


def test_a_user_shows_it_where_the_domain_hides_it(app, branded, reader):
    set_site(home_sidebar='-live_feed', community_sidebar='-live_feed')
    set_reader(reader, home_sidebar='live_feed', community_sidebar='live_feed,about_community')
    body = page(app, '/', user=reader)
    assert 'id="live_feed_card"' in body
    assert 'data-posts-url="/sidebar/live_posts"' in body and 'data-cursor="0"' in body
    assert 'js/live_feed.js' in body
    body = page(app, '/c/goldart', user=reader)
    assert body.index('id="live_feed_card"') < body.index('id="about_community"')


def test_anonymous_visitors_never_see_it(app, branded):
    set_site(home_sidebar='live_feed', community_sidebar='live_feed')
    assert 'id="live_feed_card"' not in page(app, '/')
    assert 'id="live_feed_card"' not in page(app, '/c/goldart')


def test_it_names_what_it_shows_and_links_to_the_choice(app, branded, reader):
    set_reader(reader, home_sidebar='live_feed')
    body = page(app, '/', user=reader)
    assert 'All posts' in body and '/user/settings/sidebar#live_feed_choice' in body
    set_reader(reader, sidebar_live_feed='subscribed')
    assert 'Posts from people and communities you follow' in page(app, '/', user=reader)


def test_it_listens_on_every_post_when_there_is_a_notification_server(app, branded, reader, monkeypatch):
    set_reader(reader, home_sidebar='live_feed')
    assert 'data-sse-url=""' in page(app, '/', user=reader)
    monkeypatch.setitem(app.config, 'NOTIF_SERVER', 'https://notify.example')
    assert 'data-sse-url="https://notify.example/live/stream?feed=any"' in page(app, '/', user=reader)


def test_shows_box(app, branded, reader):
    forget_login()
    with default_request(app):
        assert shows_box(HOME, 'about') and not shows_box(HOME, 'live_feed')


def test_the_dillo_theme_has_no_box(app, branded, reader):
    """dillo is for browsers without JavaScript, so it does not take the box."""
    set_site(default_theme='dillo', home_sidebar='live_feed')
    assert 'id="live_feed_card"' not in page(app, '/', user=reader)


# ---------------------------------------------------------------------------------------------------------------------
# The fragment

def test_new_posts_come_back_as_titles_with_the_cursor(home):
    first = home.post(title='First news')
    second = home.post(title='Second news')
    login(home.client, home.reader)
    response = home.client.get(f'{URL}?after={home.baseline.post2.id}')
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert item_ids(body) == [second.id, first.id]
    assert 'Second news' in body and f'href="/post/{second.id}"' in body
    assert response.headers['X-Live-Cursor'] == str(second.id)
    assert 'post_teaser' not in body


def test_nothing_new_is_204(home):
    latest = home.post()
    login(home.client, home.reader)
    response = home.client.get(f'{URL}?after={latest.id}')
    assert response.status_code == 204 and response.get_data() == b''


def test_an_old_post_is_outside_the_window(home):
    home.post(posted_at=utcnow() - timedelta(hours=2))
    login(home.client, home.reader)
    assert home.client.get(f'{URL}?after={home.baseline.post2.id}').status_code == 204


def test_at_most_the_newest_few(home):
    posts = [home.post() for _ in range(SIDEBAR_LIVE_LIMIT + 2)]
    login(home.client, home.reader)
    response = home.client.get(f'{URL}?after=0')
    assert len(item_ids(response.get_data(as_text=True))) == SIDEBAR_LIVE_LIMIT
    assert response.headers['X-Live-Cursor'] == str(posts[-1].id)


@pytest.mark.parametrize('query', ['', '?after=', '?after=abc'])
def test_a_bad_cursor_is_400(home, query):
    login(home.client, home.reader)
    assert home.client.get(f'{URL}{query}').status_code == 400


def test_anonymous_is_sent_to_log_in(home):
    response = home.client.get(f'{URL}?after=0')
    assert response.status_code == 302 and '/auth/login' in response.headers['Location']


def test_all_shows_a_community_the_reader_has_not_joined(home):
    elsewhere = make_community('elsewhere')
    elsewhere.show_all = True
    db.session.commit()
    post = make_post(elsewhere, home.author, 'https://test.piefed.local/e/1')
    login(home.client, home.reader)
    assert post.id in item_ids(home.client.get(f'{URL}?after=0').get_data(as_text=True))


def test_followed_only_leaves_out_what_the_reader_does_not_follow(home):
    joined = home.post()
    elsewhere = make_community('elsewhere')
    elsewhere.show_all = True
    db.session.commit()
    stranger = make_user(None, 'stranger', local=True)
    unfollowed = make_post(elsewhere, stranger, 'https://test.piefed.local/e/2')
    home.reader.sidebar_live_feed = 'subscribed'
    db.session.commit()
    login(home.client, home.reader)
    ids = item_ids(home.client.get(f'{URL}?after=0').get_data(as_text=True))
    assert joined.id in ids and unfollowed.id not in ids


def test_followed_only_includes_a_followed_persons_post_in_a_community_not_joined(home):
    quiet = make_community('quietplace')
    quiet.show_all = False
    db.session.commit()
    post = make_post(quiet, home.author, 'https://test.piefed.local/q/1')
    make_follow(home.reader, home.author)
    home.reader.sidebar_live_feed = 'subscribed'
    db.session.commit()
    login(home.client, home.reader)
    assert post.id in item_ids(home.client.get(f'{URL}?after=0').get_data(as_text=True))


def test_a_post_the_teaser_would_blur_is_left_out_but_the_cursor_passes_it(home):
    kept = home.post()
    blurred = home.post(nsfw=True)
    home.reader.hide_nsfw = 2
    db.session.commit()
    login(home.client, home.reader)
    response = home.client.get(f'{URL}?after={home.baseline.post2.id}')
    assert item_ids(response.get_data(as_text=True)) == [kept.id]
    assert response.headers['X-Live-Cursor'] == str(blurred.id)


def test_the_rate_budget_is_its_own(home):
    from app import create_app, limiter
    from tests.conftest import TestConfig

    class LimitedConfig(TestConfig):
        RATELIMIT_ENABLED = True

    limited = create_app(LimitedConfig)
    try:
        with limited.app_context():
            client = limited.test_client()
            login(client, home.reader)
            limiter.reset()
            every = [client.get(f'{URL}?after=0').status_code for _ in range(13)]
            home_tab = client.get('/home/live_posts/all?after=0').status_code
    finally:
        limiter.reset()
        limiter.enabled = False
    assert 429 not in every[:12] and every[12] == 429
    assert home_tab != 429


# ---------------------------------------------------------------------------------------------------------------------
# What it leaves out, and which tab

def viewer(**columns):
    values = dict(id=1, hide_nsfw=0, hide_nsfl=0, ignore_bots=0, ignore_reposters=0, hide_gen_ai=0)
    return SimpleNamespace(**{**values, **columns})


def a_post(**columns):
    values = dict(nsfw=False, nsfl=False, from_bot=False, from_reposter=False, ai_generated=False, blocked='',
                  spoiler=False)
    values.update(columns)
    return SimpleNamespace(**values, blocked_by_content_filter=lambda filters, user_id: values['blocked'],
                           spoiler_flair=lambda: values['spoiler'])


@pytest.mark.parametrize('post, user, hidden', [
    (a_post(), viewer(), False),
    (a_post(blocked='-1'), viewer(), True),
    (a_post(blocked='Filtered word'), viewer(), True),
    (a_post(nsfw=True), viewer(), False),
    (a_post(nsfw=True), viewer(hide_nsfw=2), True),
    (a_post(nsfw=True), viewer(hide_nsfw=3), True),
    (a_post(nsfl=True), viewer(hide_nsfl=2), True),
    (a_post(from_bot=True), viewer(ignore_bots=3), True),
    (a_post(from_reposter=True), viewer(ignore_reposters=2), True),
    (a_post(ai_generated=True), viewer(hide_gen_ai=2), False),   # labelled, not hidden, on a teaser
    (a_post(ai_generated=True), viewer(hide_gen_ai=3), True),
    (a_post(spoiler=True), viewer(), True),
], ids=['plain', 'keyword-hidden', 'keyword-faded', 'nsfw-shown', 'nsfw-blurred', 'nsfw-faded', 'nsfl-blurred',
        'bot-faded', 'reposter-blurred', 'ai-labelled', 'ai-faded', 'spoiler-flair'])
def test_sidebar_live_hidden(post, user, hidden):
    assert sidebar_live_hidden(post, user, {}) is hidden


@pytest.mark.parametrize('value, tab', [('all', 'all'), ('subscribed', 'subscribed'), ('local', 'all'), ('', 'all')])
def test_sidebar_live_filter(value, tab):
    assert sidebar_live_filter(SimpleNamespace(sidebar_live_feed=value)) == tab


# ---------------------------------------------------------------------------------------------------------------------
# The choice

def test_a_new_user_sees_all_posts():
    assert User.__table__.c.sidebar_live_feed.default.arg == 'all'


def test_the_settings_page_chooses_what_it_shows(app, signed_in):
    body = settings_page(signed_in)
    assert 'id="live_feed_choice"' in body
    assert re.search(r'value="live:all" class="btn btn-sm btn-primary"', body)
    response = press(signed_in, 'live:subscribed')
    assert response.status_code == 302 and response.headers['Location'].endswith('/user/settings/sidebar#live_feed_choice')
    assert reader_row().sidebar_live_feed == 'subscribed'
    assert re.search(r'value="live:subscribed" class="btn btn-sm btn-primary"', settings_page(signed_in))
    press(signed_in, 'live:all')
    assert reader_row().sidebar_live_feed == 'all'


@pytest.mark.parametrize('action', ['live:local', 'live:', 'live'])
def test_another_choice_is_refused(app, signed_in, action):
    assert press(signed_in, action).status_code == 400
    assert reader_row().sidebar_live_feed == 'all'


def test_the_choice_needs_csrf(app, signed_in):
    assert press(signed_in, 'live:subscribed', token=False).status_code == 400
    assert reader_row().sidebar_live_feed == 'all'


def test_showing_the_box_is_an_ordinary_toggle(app, signed_in):
    press(signed_in, 'home:toggle:live_feed')
    assert dict(layout(reader_row().home_sidebar, HOME))['live_feed'] is True


# ---------------------------------------------------------------------------------------------------------------------
# The migration

def test_the_migration_adds_and_drops_the_column(db_session):
    migration = load_migration('*_user_sidebar_live_feed.py')
    db.session.commit()

    def columns():
        return {column['name']: column for column in inspect(db.session.connection()).get_columns('user')}
    try:
        with Operations.context(MigrationContext.configure(db.session.connection())):
            migration.downgrade()
            assert 'sidebar_live_feed' not in columns()
            migration.upgrade()
            column = columns()['sidebar_live_feed']
            assert column['nullable'] is False and "'all'" in str(column['default'])
        db.session.commit()
    finally:
        db.session.rollback()
