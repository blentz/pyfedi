"""Admin screens v1.8.10 added: the approval queue, the liabilities filter, contacting a peer's admins, the emoji
url filter, and the vote-manipulation and posting-pattern pages.
"""
from types import SimpleNamespace

import pytest
from flask import g
from sqlalchemy.exc import OperationalError

from app import cache, db
from app.admin import routes as admin_routes
from app.constants import POST_STATUS_REVIEWING
from app.models import Conversation, Emoji, InstanceRole, Site
from app.utils import get_setting, set_setting
from tests.factories import COMMUNITY_ADMINISTRATION, grant_permission, make_community, make_instance, make_post, make_user
from tests.test_admin_content_routes import assert_refused, login
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import FIRST

pytestmark = pytest.mark.usefixtures('site')


@pytest.fixture
def admin(app, api_baseline, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the forms render form.csrf_token, as in production
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    user = make_user(api_baseline.instance_local, 'overseer', local=True)
    user.verified = True
    user.private_key = 'a key'
    db.session.commit()
    for permission in (*COMMUNITY_ADMINISTRATION, 'administer all users', 'change instance settings'):
        grant_permission(user, permission)
    return SimpleNamespace(user=user, client=Visitor(app, user, FIRST), baseline=api_baseline)


# --- the approval queue ---------------------------------------------------------------------------------------

def test_the_approval_queue_lists_held_posts_oldest_first(app, admin):
    community = make_community('talk')
    newcomer = admin.baseline.user2
    held = make_post(community, newcomer, None, title='HELDPOST')
    held.status = POST_STATUS_REVIEWING
    make_post(community, newcomer, None, title='PUBLISHEDPOST')
    db.session.commit()

    page = admin.client.get('/admin/content?show=approval').data

    assert b'HELDPOST' in page and b'PUBLISHEDPOST' not in page


def test_with_post_approval_on_the_queue_is_the_default_view(app, admin):
    set_setting('ban_posts', True)
    community = make_community('talk')
    held = make_post(community, admin.baseline.user2, None, title='HELDPOST')
    held.status = POST_STATUS_REVIEWING
    db.session.commit()

    assert b'HELDPOST' in admin.client.get('/admin/content').data


def test_with_post_approval_on_the_user_form_says_so(app, admin):
    set_setting('ban_posts', True)

    page = admin.client.get(f'/admin/user/{admin.baseline.user2.id}/edit').data

    assert b'Manually approve posts' in page


# --- instances ------------------------------------------------------------------------------------------------

def test_the_liabilities_filter(app, admin):
    risky = make_instance('risky.example', software='lemmy')
    risky.liability = True
    make_instance('fine.example', software='lemmy')
    db.session.commit()

    page = admin.client.get('/admin/instances?filter=liability').data

    assert b'risky.example' in page and b'fine.example' not in page


@pytest.fixture
def peer_admin(admin):
    peer = make_instance('peer.example', software='piefed')
    them = make_user(peer, 'peeradmin')
    db.session.add(InstanceRole(instance_id=peer.id, user_id=them.id, role='admin'))
    db.session.commit()
    return peer, them


def test_a_peers_admins_are_offered(app, admin, peer_admin):
    peer, them = peer_admin

    page = admin.client.get(f'/admin/instance/{peer.id}/contact').data

    assert f'value="{them.id}"'.encode() in page


@pytest.mark.parametrize('already_talking', [False, True])
def test_a_message_to_a_peers_admin_goes_into_one_conversation(app, admin, peer_admin, monkeypatch, already_talking):
    peer, them = peer_admin
    sent = []
    monkeypatch.setattr(admin_routes, 'send_message', lambda message, conversation_id: sent.append(
        (message, conversation_id)))
    if already_talking:
        existing = Conversation(user_id=admin.user.id)
        existing.members.append(them)
        existing.members.append(admin.user)
        db.session.add(existing)
        db.session.commit()

    response = admin.client.post(f'/admin/instance/{peer.id}/contact',
                                 data={'admin': str(them.id), 'message': 'hello there'})

    assert response.status_code == 302
    conversations = Conversation.query.all()
    assert len(conversations) == 1
    assert sent == [('hello there', conversations[0].id)]
    assert f'conversation_id={conversations[0].id}' in response.headers['Location'] or \
        f'/chat/{conversations[0].id}' in response.headers['Location']


def test_an_unknown_instance_has_no_admins_to_contact(app, admin):
    assert admin.client.get('/admin/instance/999999/contact').status_code == 404


# --- the emoji filter -----------------------------------------------------------------------------------------

def test_the_emoji_filter_is_saved_and_hides_matching_emoji(app, admin):
    db.session.add_all([Emoji(token=':ok:', url='https://cdn.example/ok.png', aliases=''),
                        Emoji(token=':bad:', url='https://bad.example/bad.png', aliases='')])
    db.session.commit()

    assert b'name="filter"' in admin.client.get('/admin/emoji/filter').data
    response = admin.client.post('/admin/emoji/filter', data={'filter': 'bad.example\n\n'})

    assert response.status_code == 302 and get_setting('emoji_filter', '') == 'bad.example\n\n'
    page = admin.client.get('/admin/emoji').data
    assert b':ok:' in page and b':bad:' not in page


# --- vote manipulation ----------------------------------------------------------------------------------------

def row(**columns):
    return SimpleNamespace(**columns)


def test_the_findings_are_folded_for_the_page(app, admin, monkeypatch):
    targeting = [row(voter_label='a@x', target_label='b@y', votes=40, downs=40, communities=5, share_pct=91.7,
                     span_seconds=90, per_min=8.0),
                 row(voter_label='c@x', target_label='d@y', votes=12, downs=0, communities=4, share_pct=60,
                     span_seconds=200000, per_min=1.0)]
    namesakes = [row(username='joe', target_label='joe@z', voter_label='joe@1', votes=5, ups=5, downs=0),
                 row(username='joe', target_label='joe@z', voter_label='joe@2', votes=7, ups=0, downs=7),
                 row(username='ann', target_label='ann@z', voter_label='ann@1', votes=9, ups=9, downs=0)]
    answers = iter([targeting, namesakes])
    monkeypatch.setattr(admin_routes, '_run_vote_query', lambda sql, params: next(answers))
    cache.delete_memoized(admin_routes._vote_manipulation_findings)

    findings = admin_routes.vote_manipulation_findings()

    assert [(t['voter'], t['against'], t['too_fast'], t['share_pct'], t['span']) for t in findings['targeting']] == [
        ('a@x', True, True, 91, '90 seconds'), ('c@x', False, False, 60, '2 days')]
    assert [(n['target'], [a['label'] for a in n['accounts']], n['votes'], n['ups'], n['downs'])
            for n in findings['namesakes']] == [('joe@z', ['joe@1', 'joe@2'], 12, 5, 7), ('ann@z', ['ann@1'], 9, 9, 0)]
    assert not findings['targeting_failed'] and not findings['namesake_failed']


@pytest.mark.parametrize('seconds, text', [(None, '0 seconds'), (119, '119 seconds'), (7199, '119 minutes'),
                                           (7200, '2 hours'), (172800, '2 days')])
def test_a_span_reads_in_the_largest_unit_that_fits(app, seconds, text):
    with app.test_request_context():
        assert admin_routes._vote_span_text(seconds) == text


def test_a_detector_that_fails_degrades_to_no_rows(app, admin, monkeypatch):
    """A statement past its budget is rolled back and answered None, so the other detector still renders."""
    def slow(*args, **kwargs):
        raise OperationalError('SELECT', {}, Exception('canceling statement due to statement timeout'))
    with app.test_request_context():
        monkeypatch.setattr(db.session, 'execute', slow)
        assert admin_routes._run_vote_query('SELECT 1', {}) is None


def test_the_votes_page_renders_and_its_refresh_starts_over(app, admin, monkeypatch):
    monkeypatch.setattr(admin_routes, '_run_vote_query', lambda sql, params: [])
    cache.delete_memoized(admin_routes._vote_manipulation_findings)

    assert admin.client.get('/admin/votes').status_code == 200
    response = admin.client.get('/admin/votes?refresh=1')
    assert response.status_code == 302 and response.headers['Location'].endswith('/admin/votes')


def test_the_real_detectors_run_on_an_empty_site(app, admin):
    cache.delete_memoized(admin_routes._vote_manipulation_findings)
    findings = admin_routes.vote_manipulation_findings()

    assert findings['targeting'] == [] and findings['namesakes'] == []
    assert not findings['targeting_failed'] and not findings['namesake_failed']


@pytest.mark.parametrize('path', ['/admin/votes_for_noone', '/admin/serial_downvoters', '/admin/high_posts_per_day',
                                  '/admin/no_link_diversity'])
def test_each_pattern_page_runs_its_query(app, admin, path):
    assert admin.client.get(path).status_code == 200


@pytest.mark.parametrize('path', ['/admin/votes', '/admin/votes_for_noone', '/admin/emoji/filter',
                                  '/admin/instance/1/contact'])
def test_the_pages_need_their_permission(app, api_baseline, path):
    g.admin_ids = []
    nobody = make_user(api_baseline.instance_local, 'nobody', local=True)
    nobody.verified = True
    db.session.commit()
    client = app.test_client()
    login(client, nobody)

    assert_refused(client.get(path))
