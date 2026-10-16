"""Fetching a local microblogs post returns the same Note send_post pushes (spec §4.1, amended §10 A1)."""
from app import db
from app.activitypub.routes import former_type_of_post
from app.activitypub.util import create_activity_of, post_to_page
from app.constants import COMMUNITY_KIND_MICROBLOGS, POST_TYPE_ARTICLE, POST_TYPE_POLL, VISIBILITY_FOLLOWERS
from tests.factories import make_community, make_instance, make_post, make_user

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


def _post(community_name='microblogs'):
    local = make_instance('test.piefed.local', software='piefed')
    alice = make_user(local, 'alice', local=True)
    post = make_post(make_community(community_name, kind=COMMUNITY_KIND_MICROBLOGS if community_name == 'microblogs' else None), alice, ap_id='https://test.piefed.local/post/1', title='Hello')
    post.type = POST_TYPE_ARTICLE
    db.session.commit()
    return post


def test_post_to_page_gives_a_note_for_microblogs(db_session):
    page = post_to_page(_post())
    assert page['type'] == 'Note' and 'name' not in page and page['content'] == '<p>Hello</p>'


def test_post_to_page_unchanged_elsewhere(db_session):
    page = post_to_page(_post('c1'))
    assert page['type'] == 'Page' and page['name'] == 'Hello'


def test_post_to_page_poll_in_microblogs_keeps_page_builder(db_session):
    post = _post()
    post.type = POST_TYPE_POLL
    db.session.commit()
    assert post_to_page(post)['type'] in ('Page', 'Question')


def test_fetch_route_serves_the_note(app, db_session):
    post = _post()
    with app.test_client() as client:
        resp = client.get(f'/post/{post.id}', headers={'Accept': 'application/activity+json'})
    assert resp.status_code == 200
    assert resp.json['type'] == 'Note' and '@context' in resp.json


def test_tombstone_former_type_is_note(db_session):
    assert former_type_of_post(_post()) == 'Note'


def test_outbox_create_wraps_the_note(db_session):
    assert create_activity_of(_post())['object']['type'] == 'Note'


def test_followers_only_microblog_fetch_matches_push_addressing(db_session):
    """post_ap serves a followers-only post to an entitled signer as addressed_as_pushed(post_to_page(post), post);
    the signer check itself is covered by tests/test_ap_security_entitlement.py, so this pins the document."""
    from app.activitypub.routes import addressed_as_pushed
    from app.activitypub.serializers import microblog_note
    post = _post()
    post.visibility = VISIBILITY_FOLLOWERS
    post.indexable = True
    db.session.commit()
    served = addressed_as_pushed(post_to_page(post), post)
    pushed = microblog_note(post, [])
    assert served == pushed
    assert served['to'] == [post.author.followers_url()]
    assert PUBLIC not in served['to'] + served['cc']
    assert 'searchableBy' not in served and 'audience' not in served
