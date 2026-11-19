"""Plain community names: without "Enable FediVerse instance details" (off by default, and for signed-out visitors)
the web view names communities by name alone, and a bare name resolves local first, then joined, then any.

Spec: docs/superpowers/specs/2026-11-19-plain-community-names-design.md
"""
import inspect

import pytest
from flask import render_template_string
from flask_login import login_user

from app import db
from app.models import Community, CommunityMember, Site
from app.plain_names import HIDE_DOMAINS_FLAG, community_href, community_label, instance_details_shown, plain_choices, \
    plain_communities, plain_community_mentions, plain_rendering, resolve_community_name, theme_community
from tests.factories import make_community, make_instance, make_user
from tests.test_microblog_live import local_instance, login

pytestmark = pytest.mark.usefixtures('db_session', 'site')


def remote_community(name, host, subscribers=0, title=None):
    instance = instance_for(host)
    community = Community(name=name, title=title or name.capitalize(), instance_id=instance.id, user_id=1,
                          ap_id=f'{name}@{host}', ap_profile_id=f'https://{host}/c/{name}',
                          ap_public_url=f'https://{host}/c/{name}', ap_domain=host,
                          subscriptions_count=0, total_subscriptions_count=subscribers, local_only=False, nsfw=False)
    db.session.add(community)
    db.session.commit()
    return community


def instance_for(host):
    from app.models import Instance
    return Instance.query.filter_by(domain=host).first() or make_instance(host, software='lemmy')


def join(user, community):
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id))
    db.session.commit()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def world(app):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = local_instance()
    make_user(local, 'founder', local=True)
    reader = make_user(local, 'reader', local=True)
    reader.private_key = 'a key'   # approval_required sends a keyless account to please_wait on a closed site
    db.session.commit()
    return reader


def as_user(app, user):
    """A request context signed in as `user` (None: signed out)."""
    ctx = app.test_request_context('/')
    ctx.push()
    if user is not None:
        login_user(user)
    return ctx


class TestPreference:

    def test_outside_a_request_details_are_shown(self, app):
        with app.app_context():
            assert instance_details_shown() is True

    def test_a_signed_out_visitor_sees_plain_names(self, app, world):
        ctx = as_user(app, None)
        try:
            assert instance_details_shown() is False
        finally:
            ctx.pop()

    def test_off_by_default_for_a_new_user(self, app, world):
        assert world.show_instance_details is False
        ctx = as_user(app, world)
        try:
            assert instance_details_shown() is False
        finally:
            ctx.pop()

    def test_on_when_the_user_enables_it(self, app, world):
        world.show_instance_details = True
        db.session.commit()
        ctx = as_user(app, world)
        try:
            assert instance_details_shown() is True
        finally:
            ctx.pop()


class TestResolve:

    def test_the_local_community_wins(self, app, world):
        local = make_community('memes')
        join(world, remote_community('memes', 'lemmy.example', subscribers=900))
        ctx = as_user(app, world)
        try:
            assert resolve_community_name('memes').id == local.id
            assert resolve_community_name('MEMES').id == local.id
        finally:
            ctx.pop()

    def test_a_joined_community_beats_a_more_popular_one(self, app, world):
        remote_community('memes', 'big.example', subscribers=900)
        joined = remote_community('memes', 'small.example', subscribers=3)
        join(world, joined)
        ctx = as_user(app, world)
        try:
            assert resolve_community_name('memes').id == joined.id
        finally:
            ctx.pop()

    def test_otherwise_the_most_subscribed(self, app, world):
        remote_community('memes', 'small.example', subscribers=3)
        big = remote_community('memes', 'big.example', subscribers=900)
        ctx = as_user(app, None)
        try:
            assert resolve_community_name('memes').id == big.id
        finally:
            ctx.pop()

    def test_banned_and_private_communities_are_passed_over(self, app, world):
        banned = remote_community('memes', 'big.example', subscribers=900)
        banned.banned = True
        private = remote_community('memes', 'secret.example', subscribers=500)
        private.private = True
        db.session.commit()
        ctx = as_user(app, None)
        try:
            assert resolve_community_name('memes') is None
        finally:
            ctx.pop()

    def test_a_handle_is_not_a_bare_name(self, app, world):
        remote_community('memes', 'big.example')
        ctx = as_user(app, None)
        try:
            assert resolve_community_name('memes@big.example') is None
            assert resolve_community_name('nothing') is None
        finally:
            ctx.pop()


class TestNames:

    def test_display_name_drops_the_domain_only_while_a_web_page_renders(self, app, world):
        remote = remote_community('memes', 'lemmy.example')
        ctx = as_user(app, None)
        try:
            assert remote.display_name() == 'Memes@lemmy.example'   # Python: notifications, flashes, the API
            assert plain_rendering() is False
            assert render_template_string('{{ c.display_name() }}', c=remote) == 'Memes'
        finally:
            ctx.pop()

    def test_display_name_keeps_the_domain_for_a_reader_who_wants_it(self, app, world):
        world.show_instance_details = True
        db.session.commit()
        remote = remote_community('memes', 'lemmy.example')
        ctx = as_user(app, world)
        try:
            assert render_template_string('{{ c.display_name() }}', c=remote) == 'Memes@lemmy.example'
            assert community_href(remote) == 'memes@lemmy.example'
            assert community_label(remote) == 'Memes@lemmy.example'
        finally:
            ctx.pop()

    def test_links_and_labels_are_bare(self, app, world):
        remote = remote_community('memes', 'lemmy.example')
        ctx = as_user(app, None)
        try:
            assert community_href(remote) == 'memes'
            assert community_label(remote) == 'Memes'
        finally:
            ctx.pop()

    def test_the_name_macro_has_no_domain(self, app, world):
        remote = remote_community('memes', 'lemmy.example')
        ctx = as_user(app, None)
        try:
            html = render_template_string('{% from "_macros.html" import render_communityname %}'
                                          '{{ render_communityname(c) }}', c=remote)
        finally:
            ctx.pop()
        assert 'href="/c/memes"' in html
        assert 'lemmy.example' not in html


class TestTheme:

    def test_a_remote_community_is_themed_as_the_local_one_of_its_name(self, app, world):
        local = make_community('memes')
        remote = remote_community('memes', 'lemmy.example')
        ctx = as_user(app, None)
        try:
            assert theme_community(remote).id == local.id
        finally:
            ctx.pop()

    def test_with_details_a_community_keeps_its_own_theme(self, app, world):
        world.show_instance_details = True
        db.session.commit()
        make_community('memes')
        remote = remote_community('memes', 'lemmy.example')
        ctx = as_user(app, world)
        try:
            assert theme_community(remote).id == remote.id
        finally:
            ctx.pop()


class TestDeduplication:

    def test_one_per_name_in_each_section_the_resolved_one_standing_for_all(self, app, world):
        small = remote_community('memes', 'small.example', subscribers=3)
        local = make_community('memes')
        other = remote_community('cats', 'lemmy.example')
        ctx = as_user(app, world)
        try:
            favorites, joined = plain_communities([small], [local, other])
        finally:
            ctx.pop()
        assert [c.id for c in favorites] == [local.id]
        assert [c.id for c in joined] == [local.id, other.id]

    def test_a_joined_community_is_kept_over_a_more_popular_one(self, app, world):
        big = remote_community('memes', 'big.example', subscribers=900)
        small = remote_community('memes', 'small.example', subscribers=3)
        join(world, small)
        ctx = as_user(app, world)
        try:
            assert [c.id for c in plain_communities([big, small])] == [small.id]
        finally:
            ctx.pop()

    def test_unchanged_for_a_reader_with_details(self, app, world):
        world.show_instance_details = True
        db.session.commit()
        small = remote_community('memes', 'small.example')
        local = make_community('memes')
        ctx = as_user(app, world)
        try:
            assert [c.id for c in plain_communities([small, local])] == [small.id, local.id]
        finally:
            ctx.pop()

    def test_post_destinations_offer_one_choice_per_name(self, app, world):
        facts = {1: ('memes', 'Memes', False, 900), 2: ('memes', 'Memes', True, 1), 3: ('cats', 'Cats', False, 1)}
        ctx = as_user(app, None)
        try:
            plain = plain_choices({'Joined communities': [(1, 'Memes@big.example')],
                                   'Others': [(2, 'Memes'), (3, 'Cats@lemmy.example')]}, facts)
        finally:
            ctx.pop()
        assert plain == {'Joined communities': [(2, 'Memes')], 'Others': [(3, 'Cats')]}


class TestMentions:

    def mentions(self, app, html):
        ctx = as_user(app, None)
        try:
            return plain_community_mentions(html)
        finally:
            ctx.pop()

    def test_a_handle_becomes_a_bare_link(self, app, world):
        remote_community('memes', 'lemmy.example')
        assert self.mentions(app, '<p>see !memes@lemmy.example now</p>') == \
            '<p>see <a href="/c/memes">!memes</a> now</p>'

    def test_a_bare_name_that_resolves_is_linked_local_first(self, app, world):
        make_community('memes')
        assert self.mentions(app, '<p>try !memes.</p>') == '<p>try <a href="/c/memes">!memes</a>.</p>'

    def test_an_exclamation_is_left_alone(self, app, world):
        assert self.mentions(app, '<p>wow!memes and !nothing here</p>') == '<p>wow!memes and !nothing here</p>'

    def test_a_peers_mention_link_is_rewritten(self, app, world):
        remote_community('memes', 'lemmy.example')
        html = '<p><a href="https://lemmy.example/c/memes">!memes@lemmy.example</a></p>'
        assert self.mentions(app, html) == '<p><a href="/c/memes">!memes</a></p>'

    def test_an_unknown_handle_loses_its_domain_but_no_link_is_made(self, app, world):
        assert self.mentions(app, '<p>!ghosts@nowhere.example</p>') == '<p>!ghosts</p>'

    def test_code_is_left_alone(self, app, world):
        remote_community('memes', 'lemmy.example')
        html = '<code>!memes@lemmy.example</code>'
        assert self.mentions(app, html) == html


class TestCommunityPage:

    def test_a_bare_name_with_no_local_community_opens_the_resolved_one(self, client, world):
        remote_community('memes', 'lemmy.example', title='Dank Memes')

        response = client.get('/c/memes')

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'Dank Memes' in html
        assert 'Dank Memes@lemmy.example' not in html

    def test_with_details_a_bare_name_is_not_resolved(self, client, world):
        world.show_instance_details = True
        db.session.commit()
        remote_community('memes', 'lemmy.example')
        login(client, world)

        assert client.get('/c/memes').status_code == 302   # to add_local, as before

    def test_activitypub_is_unaffected(self, client, world):
        remote_community('memes', 'lemmy.example')

        response = client.get('/c/memes', headers={'Accept': 'application/activity+json'})

        assert response.status_code == 404



def test_community_display_name_reads_the_flag_plain_names_sets():
    """models.py cannot import app.plain_names (a cycle), so it names the flag itself: the two must agree."""
    assert f"'{HIDE_DOMAINS_FLAG}'" in inspect.getsource(Community.display_name)
