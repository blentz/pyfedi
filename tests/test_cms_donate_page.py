"""/donate is a CMS page like any other, and any CMS page can be linked from the header and the footer.

Spec docs/superpowers/specs/2026-11-05-cms-donate-page-design.md. What a donor needs in order to pay or to stop paying
lives at /donations, in code, so that an admin deleting the CMS page cannot leave anybody unable to cancel.
"""
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from flask import current_app, g
from sqlalchemy import text

from app import cli, db
from app.constants import DONOR_WHILE_SUBSCRIBED
from app.cms import DONATE_BODY, DONATE_BODY_HTML, DONATE_TITLE, DONATE_URL, cms_nav_links
from app.models import CmsPage, MemberSubscription, Offer, Site
from app.utils import markdown_to_html
from tests.factories import make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(client=app.test_client(), member=make_user(api_baseline.instance_local, 'member', local=True))


@pytest.fixture
def stripe_configured(monkeypatch):
    monkeypatch.setitem(current_app.config, 'STRIPE_SECRET_KEY', 'sk_test')
    db.session.add(Offer(provider_kind='stripe', kind='subscription', title='A small amount', offer_group='tiers',
                         provider_price_id='price_small'))
    db.session.commit()


def sign_in(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def a_page(url='/donate', title='Donate', **flags):
    page = CmsPage(url=url, title=title, body='hello', body_html='<p>hello</p>', **flags)
    db.session.add(page)
    db.session.commit()
    return page


def donate(member):
    db.session.add(MemberSubscription(user_id=member.id, provider_kind='stripe', provider_reference='sub_1',
                                      status='active'))
    member.donor_until = DONOR_WHILE_SUBSCRIBED
    db.session.commit()


def header_of(body):
    return body[:body.index('role="contentinfo"')]


def footer_of(body):
    return body[body.index('role="contentinfo"'):]


class TestTheDonatePage:
    def test_it_is_served_from_its_page(self, env):
        a_page(title='Give us money')
        response = env.client.get('/donate')
        assert response.status_code == 200
        assert b'Give us money' in response.data and b'<p>hello</p>' in response.data

    def test_a_member_sees_the_same_page_when_stripe_is_set_up(self, env, stripe_configured):
        a_page()
        sign_in(env.client, env.member)
        response = env.client.get('/donate')
        assert b'<p>hello</p>' in response.data
        assert b'A small amount' not in response.data

    def test_a_page_has_no_sidebar_unless_it_asks_for_one(self, env):
        a_page(url='/plain', title='Plain')
        a_page(url='/wide', title='Wide', show_sidebar=True)
        assert b'id="side_pane"' not in env.client.get('/plain').data
        assert b'id="side_pane"' in env.client.get('/wide').data

    def test_a_cms_privacy_page_can_ask_for_the_sidebar_too(self, env):
        a_page(url='/privacy', title='Ours', show_sidebar=True)
        assert b'id="side_pane"' in env.client.get('/privacy').data

    def test_deleted_it_is_gone(self, env):
        assert env.client.get('/donate').status_code == 404

    def test_moved_it_answers_at_its_new_address_only(self, env):
        a_page(url='/support-us')
        assert env.client.get('/support-us').status_code == 200
        assert env.client.get('/donate').status_code == 404


class TestTheSeed:
    def test_the_stored_html_is_what_saving_the_body_in_the_editor_makes(self, app):
        with app.test_request_context():
            assert markdown_to_html(DONATE_BODY, a_target='') == DONATE_BODY_HTML

    def test_the_buttons_survive_the_sanitiser(self):
        for provider in ('https://www.patreon.com/PieFed', 'https://liberapay.com/PieFed/', 'https://ko-fi.com/piefed'):
            assert f'href="{provider}"' in DONATE_BODY_HTML
        assert 'btn btn-primary' in DONATE_BODY_HTML

    def test_it_links_to_the_plans(self):
        assert 'href="/donations"' in DONATE_BODY_HTML

    def test_the_migration_seeds_what_a_new_install_gets(self):
        migration = load_migration()
        assert (migration.TITLE, migration.URL) == (DONATE_TITLE, DONATE_URL)
        assert (migration.BODY, migration.BODY_HTML) == (DONATE_BODY, DONATE_BODY_HTML)


def load_migration():
    path = next(Path(__file__).parent.parent.glob('migrations/versions/*_cms_nav_links_and_donate_page.py'))
    spec = spec_from_file_location('cms_nav_migration', path)
    migration = module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


class TestTheMigration:
    """The test schema comes from the models, so the columns are dropped first; the rollback restores them."""

    @pytest.fixture
    def before_the_migration(self, env):
        connection = db.session.connection()
        connection.execute(text('ALTER TABLE cms_page DROP COLUMN in_header, DROP COLUMN in_footer, DROP COLUMN show_sidebar'))
        yield connection
        db.session.rollback()

    def migrate(self, connection, direction='upgrade'):
        with Operations.context(MigrationContext.configure(connection)):
            getattr(load_migration(), direction)()

    def rows(self, connection):
        return connection.execute(text('SELECT title, body, body_html, in_header, in_footer, show_sidebar FROM cms_page '
                                       "WHERE url = '/donate'")).fetchall()

    def test_a_site_without_a_page_is_given_one_linked_from_both(self, before_the_migration):
        self.migrate(before_the_migration)
        assert self.rows(before_the_migration) == [(DONATE_TITLE, DONATE_BODY, DONATE_BODY_HTML, True, True, True)]

    def test_a_site_with_its_own_page_keeps_its_words_its_links_and_its_sidebar(self, before_the_migration):
        before_the_migration.execute(text("INSERT INTO cms_page (url, title, body, body_html) "
                                          "VALUES ('/donate', 'Ours', 'ours', '<p>ours</p>')"))
        self.migrate(before_the_migration)
        assert self.rows(before_the_migration) == [('Ours', 'ours', '<p>ours</p>', True, True, True)]

    def test_any_other_page_stays_unlinked(self, before_the_migration):
        before_the_migration.execute(text("INSERT INTO cms_page (url, title, body, body_html) "
                                          "VALUES ('/rules', 'Rules', 'be kind', '<p>be kind</p>')"))
        self.migrate(before_the_migration)
        flags = before_the_migration.execute(text("SELECT in_header, in_footer, show_sidebar FROM cms_page "
                                                  "WHERE url = '/rules'")).one()
        assert tuple(flags) == (False, False, False)

    def test_the_downgrade_takes_back_the_seed_and_leaves_an_edited_page(self, before_the_migration):
        self.migrate(before_the_migration)
        self.migrate(before_the_migration, 'downgrade')
        assert self.rows_without_flags(before_the_migration) == []

        self.migrate(before_the_migration)
        before_the_migration.execute(text("UPDATE cms_page SET body = 'edited' WHERE url = '/donate'"))
        self.migrate(before_the_migration, 'downgrade')
        assert self.rows_without_flags(before_the_migration) == [('edited',)]

    def rows_without_flags(self, connection):
        return connection.execute(text("SELECT body FROM cms_page WHERE url = '/donate'")).fetchall()


class TestInitDb:
    def test_a_new_install_gets_the_page(self, app, db_session):
        cli.register(app)
        fake_db = MagicMock()
        fake_db.inspect.return_value.get_table_names.return_value = ['alembic_version']
        fake_db.session.execute.return_value.fetchone.return_value = ('UTF8',)
        with patch('app.cli.db', fake_db), patch('app.cli.retrieve_block_list', return_value=''), \
                patch('app.cli.retrieve_peertube_block_list', return_value=''):
            result = app.test_cli_runner().invoke(args=['init-db', '--interactive', 'no'])
        assert result.exception is None, result.exception
        added = [call.args[0] for call in fake_db.session.add.call_args_list]
        pages = [row for row in added if isinstance(row, CmsPage)]
        assert [(p.url, p.title, p.body, p.body_html, p.in_header, p.in_footer, p.show_sidebar) for p in pages] == [
            (DONATE_URL, DONATE_TITLE, DONATE_BODY, DONATE_BODY_HTML, True, True, True)]


class TestTheLinks:
    def test_an_opted_in_page_is_linked_by_its_title_and_url(self, env):
        a_page(url='/rules', title='House rules', in_header=True, in_footer=True)
        body = env.client.get('/keyboard_shortcuts').get_data(as_text=True)
        assert '<a class="nav-link" href="/rules">House rules</a>' in header_of(body)
        assert '<a href="/rules">House rules</a>' in footer_of(body)

    def test_a_member_sees_the_header_link_too(self, env):
        a_page(url='/rules', title='House rules', in_header=True)
        sign_in(env.client, env.member)
        assert 'href="/rules"' in header_of(env.client.get('/keyboard_shortcuts').get_data(as_text=True))

    @pytest.mark.parametrize('flags, in_header, in_footer', [
        ({}, False, False), ({'in_header': True}, True, False), ({'in_footer': True}, False, True)],
        ids=['neither', 'header', 'footer'])
    def test_each_place_is_opted_into_on_its_own(self, env, flags, in_header, in_footer):
        a_page(url='/rules', **flags)
        body = env.client.get('/keyboard_shortcuts').get_data(as_text=True)
        assert ('href="/rules"' in header_of(body)) is in_header
        assert ('href="/rules"' in footer_of(body)) is in_footer

    def test_no_donate_page_means_no_donate_link(self, env):
        assert 'href="/donate"' not in env.client.get('/keyboard_shortcuts').get_data(as_text=True)

    def test_a_title_is_escaped(self, env):
        a_page(url='/rules', title='<b>Rules</b>', in_footer=True)
        assert '<b>Rules</b>' not in env.client.get('/keyboard_shortcuts').get_data(as_text=True)

    @pytest.mark.parametrize('url, title, label', [('/about', 'About us', 'About'),
                                                    ('/privacy', 'Our privacy', 'Privacy policy')],
                             ids=['about', 'privacy'])
    @pytest.mark.parametrize('theme', ['piefed', 'dillo'])
    def test_a_page_the_footer_always_links_to_is_linked_once(self, env, url, title, label, theme):
        db.session.get(Site, 1).default_theme = theme
        a_page(url=url, title=title, in_footer=True)
        footer = footer_of(env.client.get('/keyboard_shortcuts').get_data(as_text=True))
        assert footer.count(f'href="{url}"') == 1
        assert label in footer and title not in footer

    def test_the_header_may_still_link_to_those_pages(self, env):
        a_page(url='/about', title='About us', in_header=True, in_footer=True)
        assert cms_nav_links() == {'header': [{'url': '/about', 'title': 'About us'}], 'footer': []}

    def test_the_dillo_theme_links_them_too(self, env):
        a_page(url='/up', title='Up', in_header=True)
        a_page(url='/down', title='Down', in_footer=True)
        db.session.get(Site, 1).default_theme = 'dillo'
        db.session.commit()
        body = env.client.get('/keyboard_shortcuts').get_data(as_text=True)
        assert body.index('href="/up"') < body.index('role="contentinfo"') < body.index('href="/down"')

    def test_the_lookup_lists_links_in_the_order_the_pages_were_made(self, env):
        a_page(url='/one', title='One', in_header=True, in_footer=True)
        a_page(url='/two', title='Two', in_footer=True)
        assert cms_nav_links() == {'header': [{'url': '/one', 'title': 'One'}],
                                   'footer': [{'url': '/one', 'title': 'One'}, {'url': '/two', 'title': 'Two'}]}


class TestThePlans:
    """/donations is the code's page: plans for a member, and the way to stop paying."""

    def test_a_member_is_offered_the_plans_and_no_project_links(self, env, stripe_configured):
        sign_in(env.client, env.member)
        response = env.client.get('/donations')
        assert response.status_code == 200
        assert b'A small amount' in response.data
        assert b'patreon.com' not in response.data

    def test_a_stranger_is_sent_to_log_in(self, env, stripe_configured):
        response = env.client.get('/donations')
        assert response.status_code == 302 and '/auth/login' in response.headers['Location']

    @pytest.mark.parametrize('signed_in', [False, True], ids=['stranger', 'member'])
    def test_with_no_offers_it_says_so_and_asks_nobody_to_log_in(self, env, signed_in):
        db.session.get(Site, 1).private_instance = False
        db.session.commit()
        if signed_in:
            sign_in(env.client, env.member)
        response = env.client.get('/donations')
        assert response.status_code == 404
        assert b'not available on this site' in response.data

    def test_a_donor_can_still_cancel_with_the_cms_page_deleted(self, env, stripe_configured):
        donate(env.member)
        sign_in(env.client, env.member)
        response = env.client.get('/donations')
        assert b'Cancel regular donation' in response.data

    @pytest.mark.parametrize('subscribed, label', [(False, b'Donation plans'), (True, b'Manage donations')],
                             ids=['member', 'donor'])
    def test_the_account_menu_leads_there(self, env, stripe_configured, subscribed, label):
        if subscribed:
            donate(env.member)
        sign_in(env.client, env.member)
        body = env.client.get('/keyboard_shortcuts').data
        assert label in body and b'href="/donations"' in body

    def test_the_account_menu_does_not_lead_there_without_offers(self, env):
        sign_in(env.client, env.member)
        assert b'href="/donations"' not in env.client.get('/keyboard_shortcuts').data
