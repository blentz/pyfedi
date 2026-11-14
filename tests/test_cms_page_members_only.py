"""A CMS page may be for members only: somebody not logged in is sent to log in, and is not shown links to it.

A page is public unless an admin ticks the box, so every page that existed before the column keeps being served to
everybody. /about and /privacy are served from their own routes when a CMS page replaces them, and obey it the same way.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import g
from sqlalchemy import text

from app import db
from app.cms import cms_nav_links
from app.models import CmsPage, Site
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(client=app.test_client(), member=make_user(api_baseline.instance_local, 'member', local=True))


def sign_in(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def a_page(url='/rules', title='The rules', **flags):
    page = CmsPage(url=url, title=title, body='be kind', body_html='<p>be kind</p>', **flags)
    db.session.add(page)
    db.session.commit()
    return page


def assert_sent_to_log_in(response, path):
    assert response.status_code == 302
    location = response.headers['Location']
    assert '/auth/login' in location and 'next=' in location and path.lstrip('/') in location
    assert b'be kind' not in response.data


class TestServing:
    def test_a_page_is_public_unless_it_says_otherwise(self, env):
        assert a_page().members_only is False
        response = env.client.get('/rules')
        assert response.status_code == 200 and b'be kind' in response.data

    @pytest.mark.parametrize('url', ['/rules', '/privacy', '/about'])
    def test_somebody_not_logged_in_is_sent_to_log_in(self, env, url):
        a_page(url=url, members_only=True)
        assert_sent_to_log_in(env.client.get(url), url)

    @pytest.mark.parametrize('url', ['/rules', '/privacy', '/about'])
    def test_a_member_reads_it(self, env, url):
        a_page(url=url, members_only=True)
        sign_in(env.client, env.member)
        response = env.client.get(url)
        assert response.status_code == 200 and b'be kind' in response.data

    def test_a_member_is_not_given_a_copy_a_cache_could_keep(self, env):
        a_page(members_only=True)
        sign_in(env.client, env.member)
        assert 'private' in env.client.get('/rules').headers['Cache-Control']


class TestTheLinks:
    @pytest.mark.parametrize('theme', ['piefed', 'dillo'])
    def test_somebody_not_logged_in_is_not_shown_them(self, env, theme):
        db.session.get(Site, 1).default_theme = theme
        a_page(url='/inside', title='Inside', in_header=True, in_footer=True, members_only=True)
        a_page(url='/outside', title='Outside', in_header=True, in_footer=True)
        body = env.client.get('/keyboard_shortcuts').get_data(as_text=True)
        assert 'href="/inside"' not in body
        assert body.count('href="/outside"') == 2

    @pytest.mark.parametrize('theme', ['piefed', 'dillo'])
    def test_a_member_is(self, env, theme):
        db.session.get(Site, 1).default_theme = theme
        db.session.commit()
        a_page(url='/inside', title='Inside', in_header=True, in_footer=True, members_only=True)
        sign_in(env.client, env.member)
        body = env.client.get('/keyboard_shortcuts').get_data(as_text=True)
        assert body.count('href="/inside"') == 2

    def test_the_lookup_answers_each_audience(self, env):
        a_page(url='/inside', title='Inside', in_header=True, in_footer=True, members_only=True)
        a_page(url='/outside', title='Outside', in_footer=True)
        assert cms_nav_links() == {'header': [], 'footer': [{'url': '/outside', 'title': 'Outside'}]}
        assert cms_nav_links(True) == {'header': [{'url': '/inside', 'title': 'Inside'}],
                                       'footer': [{'url': '/inside', 'title': 'Inside'},
                                                  {'url': '/outside', 'title': 'Outside'}]}


def load_migration():
    path = next(Path(__file__).parent.parent.glob('migrations/versions/*_cms_page_members_only.py'))
    spec = spec_from_file_location('cms_members_only_migration', path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


class TestTheMigration:
    """The test schema comes from the models, so the column is dropped first; the rollback restores it."""

    @pytest.fixture
    def before_the_migration(self, env):
        connection = db.session.connection()
        connection.execute(text('ALTER TABLE cms_page DROP COLUMN members_only'))
        connection.execute(text("INSERT INTO cms_page (url, title, body, body_html) "
                                "VALUES ('/rules', 'Rules', 'be kind', '<p>be kind</p>')"))
        yield connection
        db.session.rollback()

    def migrate(self, connection, direction='upgrade'):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(load_migration(), direction)()

    def columns(self, connection):
        return {row[0] for row in connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'cms_page'"))}

    def test_a_page_that_was_there_stays_public(self, before_the_migration):
        self.migrate(before_the_migration)
        assert before_the_migration.execute(text("SELECT members_only FROM cms_page WHERE url = '/rules'")).scalar() \
            is False

    def test_the_downgrade_takes_the_column_away(self, before_the_migration):
        self.migrate(before_the_migration)
        self.migrate(before_the_migration, 'downgrade')
        assert 'members_only' not in self.columns(before_the_migration)
