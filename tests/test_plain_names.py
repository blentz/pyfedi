"""Plain community names: without "Enable FediVerse instance details" (off by default, and for signed-out visitors)
the web view names communities by name alone, and a bare name resolves local first, then joined, then any.

Spec: docs/superpowers/specs/2026-11-19-plain-community-names-design.md
"""
import inspect
import re
from types import SimpleNamespace

import pytest
from sqlalchemy import String, literal, select
from sqlalchemy.exc import SQLAlchemyError
from flask import render_template_string
from flask_login import login_user

from app import cache, db, utils
from app.models import Community, CommunityMember, InstanceBlock, Site
from app.plain_names import HIDE_DOMAINS_FLAG, _key_sql, community_href, here, name_key, plain_list_clause, community_links, community_label, instance_details_shown, plain_choices, \
    plain_communities, plain_community_mentions, plain_rendering, resolve_community_name, theme_community
from tests.discovery_fixtures import fresh_cache  # noqa: F401
from tests.factories import make_community, make_instance, make_user
from tests.test_microblog_live import local_instance, login
from tests.two_domains import request_on

# fresh_cache: blocked_instances and the rest are memoized by user id, and ids repeat across tests
pytestmark = pytest.mark.usefixtures('db_session', 'site', 'fresh_cache')


def remote_community(name, host, subscribers=0, title=None, reported_total=0):
    """`subscribers` are counted here; `reported_total` is what the remote server claims."""
    instance = instance_for(host)
    community = Community(name=name, title=title or name.capitalize(), instance_id=instance.id, user_id=1,
                          ap_id=f'{name}@{host}', ap_profile_id=f'https://{host}/c/{name}',
                          ap_public_url=f'https://{host}/c/{name}', ap_domain=host,
                          subscriptions_count=subscribers, total_subscriptions_count=reported_total, local_only=False, nsfw=False)
    db.session.add(community)
    db.session.commit()
    return community


def instance_for(host):
    from app.models import Instance
    return Instance.query.filter_by(domain=host).first() or make_instance(host, software='lemmy')


def block_instance(user, instance_id):
    db.session.add(InstanceBlock(user_id=user.id, instance_id=instance_id))
    db.session.commit()
    cache.delete_memoized(utils.blocked_instances, user.id)   # a lookup earlier in the test may have cached none


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


class _Request:
    """A request in its own app context, so the login it holds (flask_login keeps it on `g`) ends with it rather than
    staying on the session-wide app context for later tests."""

    def __init__(self, app):
        self.app_ctx = app.app_context()
        self.request_ctx = app.test_request_context('/')

    def push(self):
        self.app_ctx.push()
        self.request_ctx.push()

    def pop(self):
        self.request_ctx.pop()
        self.app_ctx.pop()


def as_user(app, user):
    """A request context signed in as `user` (None: signed out)."""
    ctx = _Request(app)
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

    def test_a_remote_reported_total_cannot_take_over_a_name(self, app, world):
        """A squatter's server can claim any subscriber total; only subscribers counted here rank a community."""
        real = remote_community('memes', 'real.example', subscribers=5)
        remote_community('memes', 'squat.example', subscribers=0, reported_total=10_000_000)
        ctx = as_user(app, None)
        try:
            assert resolve_community_name('memes').id == real.id
        finally:
            ctx.pop()

    def test_on_a_tie_the_oldest_wins(self, app, world):
        first = remote_community('memes', 'first.example')
        remote_community('memes', 'later.example')
        ctx = as_user(app, None)
        try:
            assert resolve_community_name('memes').id == first.id
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

    def test_a_blocked_instances_communities_are_passed_over_joined_or_not(self, app, world):
        blocked = remote_community('memes', 'blocked.example', subscribers=900)
        join(world, blocked)
        visible = remote_community('memes', 'fine.example', subscribers=1)
        block_instance(world, blocked.instance_id)
        ctx = as_user(app, world)
        try:
            assert resolve_community_name('memes').id == visible.id
        finally:
            ctx.pop()

    def test_a_community_hidden_here_is_passed_over(self, app, world, monkeypatch):
        local = make_community('memes')
        hidden = remote_community('memes', 'hidden.example', subscribers=900)
        join(world, hidden)
        visible = remote_community('memes', 'fine.example', subscribers=1)
        hidden_ids = {local.id, hidden.id}
        monkeypatch.setattr('app.plain_names.hidden_here', lambda community: community.id in hidden_ids)
        ctx = as_user(app, world)
        try:
            assert resolve_community_name('memes').id == visible.id
        finally:
            ctx.pop()

    def test_none_when_every_candidate_is_blocked(self, app, world):
        only = remote_community('memes', 'blocked.example')
        join(world, only)
        block_instance(world, only.instance_id)
        ctx = as_user(app, world)
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

    def test_each_name_once_across_sections_the_resolved_one_standing_for_all(self, app, world):
        small = remote_community('memes', 'small.example', subscribers=3)
        local = make_community('memes')
        other = remote_community('cats', 'lemmy.example')
        ctx = as_user(app, world)
        try:
            favorites, joined = plain_communities([small], [local, other])
        finally:
            ctx.pop()
        assert [c.id for c in favorites] == [local.id]
        assert [c.id for c in joined] == [other.id]

    def test_communities_titled_alike_are_one_name(self, app, world):
        """The menu shows titles: world@ and worldnews@ both titled "World News" read as one community."""
        world_news = remote_community('world', 'lemmy.example', subscribers=50, title='World News')
        worldnews = remote_community('worldnews', 'other.example', subscribers=5, title='world  news')
        world_news_ = remote_community('world_news', 'third.example', subscribers=1, title='WORLD_NEWS')
        ctx = as_user(app, world)
        try:
            assert [c.id for c in plain_communities([worldnews, world_news_, world_news])] == [world_news.id]
        finally:
            ctx.pop()

    def test_names_alike_in_any_case_are_one_name(self, app, world):
        cats = remote_community('cats', 'lemmy.example', subscribers=9, title='Cats')
        kittens = remote_community('Cats', 'other.example', subscribers=1, title='Kitty pictures')
        ctx = as_user(app, world)
        try:
            assert [c.id for c in plain_communities([kittens, cats])] == [cats.id]
        finally:
            ctx.pop()

    def test_a_favorite_is_not_listed_again_under_joined(self, app, world):
        cats = remote_community('cats', 'lemmy.example')
        dogs = remote_community('dogs', 'lemmy.example')
        ctx = as_user(app, world)
        try:
            favorites, moderating, joined = plain_communities([cats], [], [cats, dogs])
        finally:
            ctx.pop()
        assert ([c.id for c in favorites], moderating, [c.id for c in joined]) == ([cats.id], [], [dogs.id])

    def test_different_names_and_titles_stay_apart(self, app, world):
        cats = remote_community('cats', 'lemmy.example', title='Cats')
        felines = remote_community('felines', 'lemmy.example', title='Cat lovers')
        ctx = as_user(app, world)
        try:
            assert [c.id for c in plain_communities([cats, felines])] == [cats.id, felines.id]
        finally:
            ctx.pop()

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
        # the reader's own memes stands for the name; a local memes they did not pick is left out of Others
        assert plain == {'Joined communities': [(1, 'Memes')], 'Others': [(3, 'Cats')]}

    def test_post_destinations_titled_alike_are_one_choice(self, app, world):
        facts = {1: ('world', 'World News', False, 50), 2: ('worldnews', 'world news', False, 5),
                 3: ('cats', 'Cats', False, 1)}
        ctx = as_user(app, None)
        try:
            plain = plain_choices({'Joined communities': [(2, 'world news@b.example')],
                                   'Others': [(1, 'World News@a.example'), (3, 'Cats@c.example')]}, facts)
        finally:
            ctx.pop()
        assert plain == {'Joined communities': [(2, 'world news')], 'Others': [(3, 'Cats')]}

    def test_post_destinations_are_unchanged_for_a_reader_with_details(self, app, world):
        world.show_instance_details = True
        db.session.commit()
        sections = {'Joined communities': [(1, 'Memes@a.example')], 'Others': [(2, 'Memes')]}
        ctx = as_user(app, world)
        try:
            assert plain_choices(sections, {1: ('memes', 'Memes', False, 1), 2: ('memes', 'Memes', True, 1)}) == sections
        finally:
            ctx.pop()

    def test_the_best_ranked_of_a_group_wins_wherever_it_is_listed(self, app, world):
        facts = {1: ('cats', 'Cats', True, 1), 2: ('cats', 'Cats', False, 900), 3: ('dogs', 'Dogs', False, 1)}
        ctx = as_user(app, None)
        try:
            plain = plain_choices({'Joined communities': [(1, 'Cats'), (2, 'Cats@b.example')],
                                   'Others': [(3, 'Dogs@c.example')]}, facts)
        finally:
            ctx.pop()
        assert plain == {'Joined communities': [(1, 'Cats')], 'Others': [(3, 'Dogs')]}

    def test_a_section_left_empty_is_dropped(self, app, world):
        facts = {1: ('cats', 'Cats', False, 1), 2: ('cats', 'Cats', True, 900)}
        ctx = as_user(app, None)
        try:
            plain = plain_choices({'Joined communities': [(1, 'Cats@a.example')], 'Others': [(2, 'Cats')]}, facts)
        finally:
            ctx.pop()
        assert plain == {'Joined communities': [(1, 'Cats')]}

    def test_a_strangers_community_cannot_hide_or_join_together_the_readers_own(self, app, world):
        """Others holds every community, named and titled by anyone: one named like the reader's `cats` and titled
        like their `World News` must not fold either into the other, nor take their place."""
        facts = {1: ('cats', 'Cats', False, 1), 2: ('world', 'World News', False, 1),
                 3: ('cats', 'World News', True, 999), 4: ('dogs', 'Dogs', False, 1),
                 5: ('DOGS', 'Hounds', False, 50)}
        ctx = as_user(app, None)
        try:
            plain = plain_choices({'Joined communities': [(1, 'Cats@a.example'), (2, 'World News@b.example')],
                                   'Others': [(3, 'World News'), (4, 'Dogs@c.example'), (5, 'Hounds@d.example')]},
                                  facts)
        finally:
            ctx.pop()
        assert plain == {'Joined communities': [(1, 'Cats'), (2, 'World News')], 'Others': [(5, 'Hounds')]}


class TestMentions:

    def mentions(self, app, html):
        ctx = as_user(app, None)
        try:
            return plain_community_mentions(html)
        finally:
            ctx.pop()

    def test_a_handle_loses_its_domain_but_links_to_the_community_it_names(self, app, world):
        remote_community('memes', 'lemmy.example')
        assert self.mentions(app, '<p>see !memes@lemmy.example now</p>') == \
            '<p>see <a href="/c/memes@lemmy.example">!memes</a> now</p>'

    def test_a_bare_name_that_resolves_is_linked_local_first(self, app, world):
        make_community('memes')
        assert self.mentions(app, '<p>try !memes.</p>') == '<p>try <a href="/c/memes">!memes</a>.</p>'

    def test_an_exclamation_is_left_alone(self, app, world):
        assert self.mentions(app, '<p>wow!memes and !nothing here</p>') == '<p>wow!memes and !nothing here</p>'

    def test_a_peers_mention_link_is_rewritten(self, app, world):
        remote_community('memes', 'lemmy.example')
        html = '<p><a href="https://lemmy.example/c/memes">!memes@lemmy.example</a></p>'
        assert self.mentions(app, html) == '<p><a href="/c/memes@lemmy.example">!memes</a></p>'

    def test_a_handle_never_resolves_to_another_community_of_its_name(self, app, world):
        make_community('memes')   # a local one wins the bare name, but not a handle naming the remote one
        remote_community('memes', 'lemmy.example')
        assert self.mentions(app, '<p>!memes@lemmy.example and !memes</p>') == \
            '<p><a href="/c/memes@lemmy.example">!memes</a> and <a href="/c/memes">!memes</a></p>'

    def test_an_unknown_peers_link_keeps_its_href(self, app, world):
        html = '<a href="https://nowhere.example/c/ghosts">!ghosts@nowhere.example</a>'
        assert self.mentions(app, html) == '<a href="https://nowhere.example/c/ghosts">!ghosts</a>'

    def test_an_unknown_handle_links_to_the_lookup(self, app, world):
        assert self.mentions(app, '<p>!ghosts@nowhere.example</p>') == \
            '<p><a href="/community/lookup/ghosts/nowhere.example">!ghosts</a></p>'

    def test_lookups_stop_at_the_budget_and_repeats_are_free(self, app, world, monkeypatch):
        """A comment full of distinct names costs at most MENTION_LOOKUPS lookups; a name seen before costs none."""
        monkeypatch.setattr('app.plain_names.MENTION_LOOKUPS', 2)
        make_community('memes')
        remote_community('cats', 'lemmy.example')
        ctx = as_user(app, None)
        try:
            html = plain_community_mentions('<p>!memes !memes !cats@lemmy.example !dogs !memes !birds@nowhere.example</p>')
        finally:
            ctx.pop()
        assert html == ('<p><a href="/c/memes">!memes</a> <a href="/c/memes">!memes</a> '
                        '<a href="/c/cats@lemmy.example">!cats</a> !dogs <a href="/c/memes">!memes</a> '
                        '<a href="/community/lookup/birds/nowhere.example">!birds</a></p>')

    def test_nothing_to_rewrite(self, app, world):
        assert self.mentions(app, '') == ''
        assert self.mentions(app, None) is None

    def test_a_handle_mentioned_twice_is_looked_up_once(self, app, world, monkeypatch):
        remote_community('cats', 'lemmy.example')
        monkeypatch.setattr('app.plain_names.MENTION_LOOKUPS', 1)
        assert self.mentions(app, '<p>!cats@lemmy.example !cats@lemmy.example</p>') == \
            '<p><a href="/c/cats@lemmy.example">!cats</a> <a href="/c/cats@lemmy.example">!cats</a></p>'

    def test_a_reader_with_details_gets_the_lookup_links(self, app, world):
        world.show_instance_details = True
        db.session.commit()
        ctx = as_user(app, world)
        try:
            html = community_links('<p>!cats@lemmy.example</p>')
        finally:
            ctx.pop()
        assert html == '<p><a href="https://test.piefed.local/community/lookup/cats/lemmy.example">!cats@lemmy.example</a></p>'

    def test_the_template_filter_gives_a_plain_reader_plain_mentions(self, app, world):
        remote_community('cats', 'lemmy.example')
        ctx = as_user(app, None)
        try:
            html = render_template_string('{{ body | community_links | safe }}', body='<p>!cats@lemmy.example</p>')
        finally:
            ctx.pop()
        assert html == '<p><a href="/c/cats@lemmy.example">!cats</a></p>'

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

    def test_a_picked_community_is_accepted_though_another_wins_its_name(self, client, world):
        """The post chooser lists the reader's communities, grouped among themselves; the pick is checked against
        every community they may post to, not that list grouped among all of them, where a local one wins."""
        local = make_community('world')
        local.title = 'World News'
        joined = remote_community('worldnews', 'lemmy.example', title='World News')
        join(world, joined)
        login(client, world)

        response = client.get(f'/add_post?community={joined.id}')

        assert response.status_code == 302
        assert '/community/worldnews@lemmy.example/submit' in response.headers['Location']

    def test_activitypub_is_unaffected(self, client, world):
        remote_community('memes', 'lemmy.example')

        response = client.get('/c/memes', headers={'Accept': 'application/activity+json'})

        assert response.status_code == 404



def browse_labels(html):
    """The rows of the All Communities list, by the label of the link to each."""
    return re.findall(r'aria-label="Browse ([^"]*)"', html)


class TestCommunityList:
    """/communities lists each name once, the community `_rank` puts first standing for the rest."""

    def test_a_search_lists_a_shared_name_once(self, client, world):
        hosts = [f'host{n}.example' for n in range(7)]
        communities = [remote_community('technology', host, subscribers=n, title='Technology')
                       for n, host in enumerate(hosts)]

        html = client.get('/communities?search=Technology').get_data(as_text=True)

        assert browse_labels(html) == ['Technology']
        assert f'/community/{communities[-1].link()}/subscribe' in html   # the most subscribed here stands for all

    def test_titles_and_names_alike_in_any_case_are_one_row(self, client, world):
        remote_community('technology', 'a.example', subscribers=5, title='Technology')
        remote_community('tech', 'b.example', title='TECHNOLOGY')
        remote_community('Technology', 'c.example', title='Tech talk')

        assert browse_labels(client.get('/communities?search=tech').get_data(as_text=True)) == ['Technology']

    def test_the_local_community_stands_for_its_name(self, client, world):
        make_community('technology').title = 'Technology'
        db.session.commit()
        remote_community('technology', 'big.example', subscribers=900, title='Technology')

        html = client.get('/communities?search=technology').get_data(as_text=True)

        assert browse_labels(html) == ['Technology']
        assert '/community/technology/subscribe' in html and 'technology@big.example' not in html

    def test_a_joined_community_stands_for_its_name(self, client, world):
        remote_community('technology', 'big.example', subscribers=900, title='Technology')
        joined = remote_community('technology', 'small.example', subscribers=1, title='Technology')
        join(world, joined)
        login(client, world)

        html = client.get('/communities?search=technology').get_data(as_text=True)

        assert browse_labels(html) == ['Technology']
        assert 'technology@small.example' in html and 'technology@big.example' not in html

    def test_different_names_are_all_listed(self, client, world):
        remote_community('technology', 'a.example', title='Technology')
        remote_community('technews', 'a.example', title='Technology news')

        html = client.get('/communities?search=technology').get_data(as_text=True)

        assert sorted(browse_labels(html)) == ['Technology', 'Technology news']

    def test_the_count_and_pages_follow_the_deduplicated_list(self, client, world):
        for n in range(60):
            remote_community('technology', f'host{n}.example', title='Technology')

        html = client.get('/communities?search=technology').get_data(as_text=True)

        assert browse_labels(html) == ['Technology'] and 'page=2' not in html

    def test_with_details_every_community_is_listed(self, client, world):
        world.show_instance_details = True
        db.session.commit()
        for host in ('a.example', 'b.example', 'c.example'):
            remote_community('technology', host, title='Technology')
        login(client, world)

        html = client.get('/communities?search=technology').get_data(as_text=True)

        assert sorted(browse_labels(html)) == ['Technology@a.example', 'Technology@b.example', 'Technology@c.example']

    def test_no_clause_with_details(self, app, world):
        world.show_instance_details = True
        db.session.commit()
        ctx = as_user(app, world)
        try:
            assert plain_list_clause(Community.query) is None
        finally:
            ctx.pop()

    def test_the_winner_of_each_title_and_name_is_kept(self, app, world):
        winner = remote_community('technology', 'a.example', subscribers=9)
        losers = [remote_community('technology', 'b.example'), remote_community('Technology', 'c.example', title='x'),
                  remote_community('tech', 'd.example', title='TECHNOLOGY')]
        apart = remote_community('cats', 'a.example')
        kept = kept_ids(app, world, Community.query.filter(Community.id.in_([winner.id, apart.id] + [c.id for c in losers])))
        assert kept == sorted([winner.id, apart.id])

    def test_a_joined_community_is_kept_over_a_more_popular_one(self, app, world):
        remote_community('technology', 'big.example', subscribers=900)
        joined = remote_community('technology', 'small.example', subscribers=1)
        join(world, joined)
        assert kept_ids(app, world, Community.query.filter(Community.name == 'technology')) == [joined.id]

    def test_a_chain_lists_no_name_twice(self, app, world):
        """A shares B's title and B shares C's name: whatever is kept, no two kept read the same or share a name."""
        a = remote_community('alpha', 'a.example', subscribers=1, title='Shared')
        b = remote_community('beta', 'b.example', subscribers=2, title='Shared')
        c = remote_community('beta', 'c.example', subscribers=3, title='Other')
        kept = kept_ids(app, world, Community.query.filter(Community.id.in_([a.id, b.id, c.id])))
        rows = [db.session.get(Community, community_id) for community_id in kept]
        assert len({name_key(r.title) for r in rows}) == len(rows) == len({name_key(r.name) for r in rows})
        assert c.id in kept   # the first in `_rank`'s order is always kept

    @pytest.mark.parametrize('text', ['World News', 'world_news', 'WORLD  news', ' world-_- news ', 'Ünïcode Ä', '', None])
    def test_the_database_reads_a_name_as_name_key_does(self, app, text):
        assert db.session.scalar(select(_key_sql(literal(text, String)))) == name_key(text)


def kept_ids(app, user, query):
    """The ids of `query` that `plain_list_clause` keeps for `user`."""
    ctx = as_user(app, user)
    try:
        return sorted(c.id for c in query.filter(plain_list_clause(query)))
    finally:
        ctx.pop()


class TestStorefronts:
    """On one of several domains this install serves, "local" is local to that domain, as `local_clause` reads a bare
    name: a community on a sister domain does not stand for the name there."""

    @pytest.fixture
    def arts(self, two_domains):
        first, onion = two_domains.domains
        first_art = two_domains.local_community('art', first)   # older, and more subscribed
        first_art.subscriptions_count = 900
        onion_art = two_domains.local_community('art', onion)
        db.session.commit()
        return SimpleNamespace(first=first, onion=onion, first_art=first_art, onion_art=onion_art)

    @pytest.mark.parametrize('on', ['first', 'onion'])
    def test_the_menu_keeps_this_domains_community(self, app, arts, on):
        with request_on(app, getattr(arts, on)):
            kept = plain_communities([arts.first_art, arts.onion_art])
        assert [c.id for c in kept] == [getattr(arts, f'{on}_art').id]

    @pytest.mark.parametrize('on', ['first', 'onion'])
    def test_the_list_keeps_this_domains_community(self, app, arts, on):
        winner = arts.first_art if on == 'first' else arts.onion_art
        query = Community.query.filter(Community.name == 'art')
        with request_on(app, getattr(arts, on)):
            assert [c.id for c in query.filter(plain_list_clause(query))] == [winner.id]

    def test_here(self, app, arts):
        remote = SimpleNamespace(ap_id='art@lemmy.example', site_domain_id=None)
        unstamped = SimpleNamespace(ap_id=None, site_domain_id=None)
        with request_on(app, arts.first):
            assert (here(arts.first_art), here(arts.onion_art), here(unstamped), here(remote)) == \
                (True, False, True, False)   # the default domain's own include the rows no domain was stamped on
        with request_on(app, arts.onion):
            assert (here(arts.first_art), here(arts.onion_art), here(unstamped), here(remote)) == \
                (False, True, False, False)


def test_community_display_name_reads_the_flag_plain_names_sets():
    """models.py cannot import app.plain_names (a cycle), so it names the flag itself: the two must agree."""
    assert f"'{HIDE_DOMAINS_FLAG}'" in inspect.getsource(Community.display_name)


def test_a_page_renders_in_todays_view_when_the_reader_cannot_be_read(app, world, monkeypatch):
    """An error page after a failed transaction: reading the reader's setting fails, and the page still renders."""
    def broken():
        raise SQLAlchemyError('the transaction is gone')

    remote = remote_community('memes', 'lemmy.example')
    monkeypatch.setattr('app.plain_names.instance_details_shown', broken)
    ctx = as_user(app, None)
    try:
        assert render_template_string('{{ c.display_name() }}', c=remote) == 'Memes@lemmy.example'
    finally:
        ctx.pop()
