"""v1.8.10's admin analytics name accounts from raw SQL: /admin/votes (vote targeting and namesakes),
/admin/votes_for_noone, /admin/serial_downvoters, /admin/high_posts_per_day, /admin/no_link_diversity, and an
account's /u/<actor>/voting_patterns (the accounts it votes on and is voted on by).

No clearnet viewer, admins included, sees an onion account (follow-up F6 ruling; owner ruling 2026-11-01 that these
pages follow it). Each list leaves out a row naming an account hidden here, as voter or as target, and the onion
still sees its own. /admin/votes' findings are cached for an hour, so each storefront has its own entry: the onion's
must never be served on the clearnet.
"""
from datetime import timedelta

import pytest
from cachelib import SimpleCache

from app import cache, db
from app.constants import POST_TYPE_LINK
from app.models import Community, Domain, utcnow
from tests.factories import grant_permissions, COMMUNITY_ADMINISTRATION, grant_permission, make_post, make_post_vote
from tests.test_instance_visible_here import scene  # noqa: F401 -- scene is a fixture
from tests.test_per_domain_ap_routes import onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST, ONION, two_domains  # noqa: F401

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def admins(app, scene, monkeypatch):  # noqa: F811
    """first_bob administers on the first domain, onion_bob on the onion; a real cache, as production has."""
    monkeypatch.setitem(app.extensions['cache'], cache, SimpleCache())
    for user in (scene.first_bob, scene.onion_bob):
        grant_permissions(user, *COMMUNITY_ADMINISTRATION)
        grant_permission(user, 'manage users')
    return scene


def seen_by(app, monkeypatch, user, host, path):
    """The template context `path` renders for `user` on `host`."""
    seen = {}

    def capture(template, **context):
        seen.update(context)
        return 'rendered'
    monkeypatch.setattr('app.admin.routes.render_template', capture)
    monkeypatch.setattr('app.user.routes.render_template', capture)
    assert Visitor(app, user, host).get(path).status_code == 200
    return seen


def on_both(app, monkeypatch, scene, path):
    """What the onion's admin is shown, then the first domain's: in that order, so a shared cache would leak."""
    return (seen_by(app, monkeypatch, scene.onion_bob, ONION, path),
            seen_by(app, monkeypatch, scene.first_bob, FIRST, path))


def burst(voter, author, n=10):
    """`voter` downvotes `n` of `author`'s posts, in the author's own domain's community, within the minute: serial
    downvoting, targeting (every vote on one account, too fast to be human) and, as both are called bob, a namesake."""
    community = Community.query.filter(Community.site_domain_id == author.site_domain_id,
                                       Community.title.like('% art')).one()
    # One clock minute, whenever the test runs: vote_targeting.sql's rate is votes over the distinct minutes they fall
    # in, so ten votes across a minute's end read as five a minute, under its six, and the test failed now and then
    minute = utcnow().replace(second=0, microsecond=0) - timedelta(minutes=1)
    for i in range(n):
        post = make_post(community, author, f'https://{community.id}.example/post/{author.id}-{i}', title=f'b{i}')
        make_post_vote(voter, post, -1).created_at = minute + timedelta(seconds=i)
    db.session.commit()


DIRECTIONS = ['onion voter', 'onion target']


def pair(scene, direction):
    """(voter, author)"""
    if direction == 'onion voter':
        return scene.onion_bob, scene.first_bob
    return scene.first_bob, scene.onion_bob


@pytest.mark.parametrize('direction', DIRECTIONS)
def test_serial_downvoters_name_no_onion_account_on_the_clearnet(app, admins, monkeypatch, direction):
    burst(*pair(admins, direction))

    onion, first = on_both(app, monkeypatch, admins, '/admin/serial_downvoters')

    voter, author = pair(admins, direction)
    assert [(row.user_id, row.author_id) for row in onion['rows']] == [(voter.id, author.id)]
    assert first['rows'] == []


@pytest.mark.parametrize('direction', DIRECTIONS)
def test_vote_findings_name_no_onion_account_on_the_clearnet(app, admins, monkeypatch, direction):
    burst(*pair(admins, direction))

    onion, first = on_both(app, monkeypatch, admins, '/admin/votes')

    assert len(onion['findings']['targeting']) == 1 and len(onion['findings']['namesakes']) == 1
    assert first['findings']['targeting'] == [] and first['findings']['namesakes'] == []


def test_the_refresh_starts_every_storefront_over(app, admins, monkeypatch):
    seen_by(app, monkeypatch, admins.onion_bob, ONION, '/admin/votes')   # cached empty
    burst(admins.onion_bob, admins.first_bob)
    assert Visitor(app, admins.first_bob, FIRST).get('/admin/votes?refresh=1').status_code == 302

    onion = seen_by(app, monkeypatch, admins.onion_bob, ONION, '/admin/votes')

    assert len(onion['findings']['targeting']) == 1


def test_accounts_that_vote_for_no_one_leave_out_the_onions(app, admins, monkeypatch):
    for i in range(11):
        make_post(admins.onion_art, admins.onion_bob, f'http://{ONION}/post/quiet{i}', title=f'q{i}')

    onion, first = on_both(app, monkeypatch, admins, '/admin/votes_for_noone')

    assert [row.id for row in onion['rows']] == [admins.onion_bob.id]
    assert first['rows'] == []


def test_high_posts_per_day_leaves_out_the_onions(app, admins, monkeypatch):
    onion, first = on_both(app, monkeypatch, admins, '/admin/high_posts_per_day')   # the scene's ONIONPOST

    assert admins.onion_bob.id in [row.user_id for row in onion['rows']]
    assert admins.onion_bob.id not in [row.user_id for row in first['rows']]


def test_no_link_diversity_leaves_out_the_onions(app, admins, monkeypatch):
    domain = Domain(name='one.example')
    db.session.add(domain)
    db.session.commit()
    post = make_post(admins.onion_art, admins.onion_bob, f'http://{ONION}/post/link', title='link')
    post.type, post.domain_id = POST_TYPE_LINK, domain.id
    admins.onion_bob.post_count, admins.onion_bob.post_reply_count = 201, 0
    admins.onion_bob.last_seen = utcnow() - timedelta(days=1)
    db.session.commit()

    onion, first = on_both(app, monkeypatch, admins, '/admin/no_link_diversity')

    assert [row.user_name for row in onion['rows']] == [admins.onion_bob.user_name]
    assert first['rows'] == []


@pytest.mark.parametrize('direction', DIRECTIONS)
def test_an_accounts_voting_patterns_name_no_onion_account_on_the_clearnet(app, admins, monkeypatch, direction):
    burst(*pair(admins, direction))
    path = f'/u/{admins.first_bob.user_name}/voting_patterns'

    first = seen_by(app, monkeypatch, admins.first_bob, FIRST, path)

    assert first['votes_received'] == [] and first['votes_cast'] == []


def test_voting_patterns_still_name_the_onions_accounts_on_the_onion(app, admins, monkeypatch):
    burst(admins.first_bob, admins.onion_bob)

    onion = seen_by(app, monkeypatch, admins.onion_bob, ONION, f'/u/{admins.onion_bob.user_name}/voting_patterns')

    assert [row.user_id for row in onion['votes_received']] == [admins.first_bob.id]

