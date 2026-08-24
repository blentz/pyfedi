import pytest
from sqlalchemy import text

from app import db
from app.models import User


def test_api_user_subscriptions(app, api_baseline):
    from app.api.alpha.utils.user import put_user_subscribe

    user_id = api_baseline.user1.id
    user = User.query.get(user_id)
    assert user is not None and hasattr(user, 'id')
    jwt = user.encode_jwt_token()
    assert jwt is not None
    auth = f'Bearer {jwt}'

    # normal add / remove subscription
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 0'),
        {"user_id": user_id}).scalars()
    # Users who have blocked user_id are excluded too: without it, an unordered
    # `.first()` over a freshly seeded table could hand back someone who has
    # blocked this user, which put_user_subscribe rejects with "This user has
    # blocked you." -- breaking the "normal add/remove" flow below before it
    # ever gets to the block case this test exercises explicitly further down.
    existing_bans = db.session.execute(text('SELECT blocker_id FROM "user_block" WHERE blocked_id = :user_id'),
                                       {"user_id": user_id}).scalars()
    # User.id != user_id: without it, an unordered `.first()` over a freshly seeded
    # table is liable to hand back user_id itself (the smallest id, physically first
    # in the table) as "a person to subscribe to" -- which put_user_subscribe rejects
    # as a self-subscription, breaking the "normal add/remove" flow below before it
    # ever gets to the self-subscribe case this test exercises explicitly further
    # down. Excluding self here makes the query say what it means: find someone else.
    person = User.query.filter(User.id.not_in(existing_subs), User.id.not_in(existing_bans),
                               User.id != user_id, User.banned == False).first()
    assert person is not None and hasattr(person, 'id')

    data = {"person_id": person.id, "subscribe": True}
    result = put_user_subscribe(auth, data)
    assert result is not None and result['person_view']['activity_alert'] == True
    data = {"person_id": person.id, "subscribe": False}
    result = put_user_subscribe(auth, data)
    assert result is not None and result['person_view']['activity_alert'] == False

    # remove from non-existing
    data = {"person_id": person.id, "subscribe": False}
    with pytest.raises(Exception) as ex:
        put_user_subscribe(auth, data)
    assert str(ex.value) == 'A subscription for this user did not exist.'

    # add to existing
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 0'),
        {"user_id": user_id}).scalars()
    person = User.query.filter(User.id.in_(existing_subs), User.banned == False).first()
    assert person is not None and hasattr(user, 'id')
    if user:
        data = {"person_id": person.id, "subscribe": True}
        with pytest.raises(Exception) as ex:
            put_user_subscribe(auth, data)
        assert str(ex.value) == 'A subscription for this user already existed.'

    # add to a banned user
    person = User.query.filter(User.banned == True).first()
    assert person is not None
    if person:
        data = {"person_id": person.id, "subscribe": True}
        with pytest.raises(Exception):
            result = put_user_subscribe(auth, data)

    # remove from a banned user
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 0'),
        {"user_id": user_id}).scalars()
    person = User.query.filter(User.id.in_(existing_subs), User.banned == True).first()
    if person:
        data = {"person_id": person.id, "subscribe": False}
        with pytest.raises(Exception):
            result = put_user_subscribe(auth, data)

    # subscribe to self
    data = {"person_id": user_id, "subscribe": True}
    with pytest.raises(Exception):
        result = put_user_subscribe(auth, data)

    # subscribe to a user who has blocked this user
    existing_bans = db.session.execute(text('SELECT blocker_id FROM "user_block" WHERE blocked_id = :user_id'),
                                       {"user_id": user_id}).scalars()
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 0'),
        {"user_id": user_id}).scalars()
    person = User.query.filter(User.id.in_(existing_bans), User.id.not_in(existing_subs),
                               User.banned == False).first()
    assert person is not None
    data = {"person_id": person.id, "subscribe": True}
    with pytest.raises(Exception) as ex:
        result = put_user_subscribe(auth, data)
    assert str(ex.value) == 'This user has blocked you.'
