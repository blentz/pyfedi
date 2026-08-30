"""Sub-project 5b, Task 5 -- FIX 1: the a.gup.pe string Accept cannot succeed.

routes.py:1076 states the contract in its own comment: "we have two user
variables in play -- user and requestor_user! requestor_user is the one [who]
made the follow request originally while user is the one who sent the
Accept". a.gup.pe accepts a follow by sending the follow request's own ID as
a bare string (routes.py:1077-1085) rather than embedding the Follow object
the way Lemmy does. That branch looks up the CommunityJoinRequest by the
string's last path segment and, before the fix, assigned its user to `user`
-- backwards twice per the comment above: it leaves `requestor_user` unset
(so the very next line, `if not requestor_user:` at :1091, is always true)
and it clobbers whatever `user` already held (the Accept's sender, which the
`elif user:` branch at :1130 would otherwise consume for a non-Community,
non-Feed recipient). The path therefore ALWAYS logged 'Could not find
recipient of Accept' and returned, discarding the lookup's result on every
call -- a permanently dead branch, not merely a rare miss.

Step 1 finding (recorded here, not asserted by these tests): a throwaway
probe (find_actor_or_create_cached wrapped and its calls recorded, activity
built via inbox_activity(community, activity_type='Accept', ...) against a
Community seeded with ap_fetched_at stamped) showed exactly ONE preamble
call -- find_actor_or_create_cached('https://peer.example/c/microblogs',
community_only=True, create_if_not_found=False) -> a Community instance.
Because that first lookup (routes.py:862) hit, the feed_only and unnarrowed
lookups (864, 866) never ran (see test_inbox_dispatch_preamble.py's Task 2
outcome-table comment for why a truthy `community` short-circuits both). So
for a.gup.pe's real shape -- Accept from a Community actor -- `community` is
set and `feed`/`user` are None, meaning the fixed path in every test below
reaches `if community:` at :1095, exactly as the task brief assumed. The
probe file itself was deleted after use; it changed nothing under `app/`.
"""
from app import db
from app.models import ActivityPubLog, CommunityMember, utcnow
from tests.factories import inbox_activity, make_community, make_community_join_request, \
    make_instance, make_user
from tests.test_inbox_dispatch_preamble import dispatch


def _seed_agupe_community(host='peer.example'):
    """A remote Community an a.gup.pe-shaped Accept's actor resolves to.

    ap_fetched_at is stamped so find_actor_or_create_cached's
    schedule_actor_refresh does not fire a real actor fetch inline under
    eager Celery. make_community() hardcodes owner user_id=1 and
    instance_id=1, so an instance and a local user are seeded first to
    occupy those ids (same pattern as test_inbox_dispatch_preamble.py's
    Task 2 community test).
    """
    instance = make_instance(host)
    make_user(None, 'community_owner', local=True)
    community = make_community(host=host)
    community.ap_fetched_at = utcnow()
    db.session.commit()
    return community, instance


def test_an_agupe_string_accept_admits_the_join_requests_user(app, db_session, monkeypatch):
    """routes.py:1077-1085 (fixed) + :1095-1117. a.gup.pe accepts by sending
    the follow request's ID as a bare string rather than embedding the
    Follow object. The activity's string object is a URL whose LAST path
    segment is the join request's `uuid` (make_community_join_request's
    docstring), matching :1078-1080's split-on-'/' lookup.

    Before the fix (see module docstring): the lookup's result was assigned
    to `user` instead of `requestor_user`, so :1091's `if not requestor_user:`
    was always true and the path logged 'Could not find recipient of Accept'
    without ever reaching :1095. This test asserts the CORRECTED behaviour:
    the join request's user becomes a CommunityMember of the community.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    joiner = make_user(instance, 'joiner')
    join_request = make_community_join_request(joiner, community)

    activity = inbox_activity(
        community, activity_type='Accept',
        object=f'https://peer.example/activities/follow/{join_request.uuid}')

    dispatch(activity)

    member = db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).first()
    assert member is not None
    assert ActivityPubLog.query.one().result == 'success'


def test_an_agupe_numeric_style_accept_retries_by_primary_key(app, db_session, monkeypatch):
    """routes.py:1081-1083. Old-style a.gup.pe join requests were identified
    by a bare integer rather than a uuid -- the string object's last path
    segment is the join request's integer `id`, which is not a valid uuid.
    The `.filter_by(uuid=...)` lookup at :1080 raises (Postgres rejects the
    non-uuid literal for a uuid column), :1081's `except Exception:` catches
    it, :1082 rolls back the aborted transaction, and :1083 retries with
    `.get()` against the primary key instead -- finding the SAME row by a
    different column. This test gives the activity a string object ending in
    the join request's `id`, killing that retry path specifically (a uuid
    would never reach it).
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)
    community, instance = _seed_agupe_community()
    joiner = make_user(instance, 'joiner')
    join_request = make_community_join_request(joiner, community)

    activity = inbox_activity(
        community, activity_type='Accept',
        object=f'https://peer.example/activities/follow/{join_request.id}')

    dispatch(activity)

    member = db.session.query(CommunityMember).filter_by(
        user_id=joiner.id, community_id=community.id).first()
    assert member is not None
    assert ActivityPubLog.query.one().result == 'success'
