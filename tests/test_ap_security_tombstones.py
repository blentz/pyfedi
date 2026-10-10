"""S3.2: deleted local actors answer 410 Tombstone; bans stay a reversible 404 (spec 2026-10-10)."""
from app import db
from tests.factories import make_community, make_instance, make_user
from tests.test_actor_profiles import seed_actors
from tests.test_ap_content_objects import ap_get, browser_get


def _local_user(name, *, deleted=False, banned=False, bot=False):
    seed_actors('test.piefed.local')
    user = make_user(None, name, local=True)
    user.deleted = deleted
    user.banned = banned
    user.bot = bot
    db.session.commit()
    return user


def test_a_deleted_local_user_is_a_person_tombstone(app, db_session):
    _local_user('gone', deleted=True, banned=True)   # what account deletion writes

    response = ap_get(app, '/u/gone')

    assert response.status_code == 410
    assert response.content_type == 'application/activity+json'
    assert response.headers['Cache-Control'] == 'no-store'
    assert response.json['type'] == 'Tombstone'
    assert response.json['formerType'] == 'Person'
    assert response.json['id'] == 'https://test.piefed.local/u/gone'


def test_the_tombstone_lookup_ignores_the_case_of_the_name(app, db_session):
    _local_user('gone', deleted=True, banned=True)

    assert ap_get(app, '/u/GONE').status_code == 410


def test_a_deleted_local_bot_is_a_service_tombstone(app, db_session):
    _local_user('gonebot', deleted=True, banned=True, bot=True)

    response = ap_get(app, '/u/gonebot')

    assert response.status_code == 410
    assert response.json['formerType'] == 'Service'


def test_a_banned_local_user_stays_404_so_the_ban_is_reversible(app, db_session):
    _local_user('benched', banned=True)

    assert ap_get(app, '/u/benched').status_code == 404


def test_a_browser_asking_for_a_deleted_user_is_unchanged(app, db_session):
    _local_user('gone', deleted=True, banned=True)

    assert browser_get(app, '/u/gone').status_code == 404


def test_an_unknown_local_name_is_still_404(app, db_session):
    seed_actors('test.piefed.local')

    assert ap_get(app, '/u/nobody').status_code == 404


def test_a_deleted_or_banned_community_still_answers_404(app, db_session):
    """Local community deletion sets banned=True and stays restorable for 7 days (app/shared/community.py:653),
    so a 410 would tell peers to purge a community that may come back."""
    seed_actors('test.piefed.local')
    community = make_community(name='dead')
    community.banned = True
    db.session.commit()

    assert ap_get(app, '/c/dead').status_code == 404


def test_a_deleted_remote_user_sharing_the_name_is_not_tombstoned_by_us(app, db_session):
    """We only vouch for our own accounts: a remote row named 'gone' must not turn /u/gone into a Tombstone.
    resolve_remote_handle returns None for a bare name, so nothing touches the network."""
    seed_actors('test.piefed.local')
    remote = make_user(make_instance('other.example'), 'gone')
    remote.deleted = True
    db.session.commit()
    assert remote.ap_id == 'gone@other.example'

    assert ap_get(app, '/u/gone').status_code == 404


def test_a_head_for_a_deleted_local_user_is_a_tombstone_too(app, db_session):
    _local_user('gone', deleted=True, banned=True)

    with app.test_client() as client:
        response = client.head('/u/gone', headers={'Accept': 'application/activity+json'})

    assert response.status_code == 410
