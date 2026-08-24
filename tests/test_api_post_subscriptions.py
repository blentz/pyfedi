import pytest
from flask import g
from sqlalchemy import text

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import Post, User


def test_api_post_subscriptions(app, api_baseline):
    from app.api.alpha.utils.post import put_post_subscribe

    # post_view (called by put_post_subscribe) unconditionally reads g.admin_ids.
    # See test_api_post_bookmarks.py for why this must be set explicitly here.
    g.admin_ids = []

    user_id = api_baseline.user1.id
    user = User.query.get(user_id)
    assert user is not None and hasattr(user, 'id')
    jwt = user.encode_jwt_token()
    assert jwt is not None
    auth = f'Bearer {jwt}'

    # normal add / remove subscription
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 3'),
        {"user_id": user_id}).scalars()
    post = Post.query.filter(Post.id.not_in(existing_subs), Post.deleted == False,
                             Post.status > POST_STATUS_REVIEWING).first()
    assert post is not None and hasattr(post, 'id')

    data = {"post_id": post.id, "subscribe": True}
    result = put_post_subscribe(auth, data)
    assert result is not None and result['post_view']['activity_alert'] == True
    data = {"post_id": post.id, "subscribe": False}
    result = put_post_subscribe(auth, data)
    assert result is not None and result['post_view']['activity_alert'] == False

    # remove from non-existing
    data = {"post_id": post.id, "subscribe": False}
    with pytest.raises(Exception) as ex:
        put_post_subscribe(auth, data)
    assert str(ex.value) == 'A subscription for this post did not exist.'

    # add to existing
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 3'),
        {"user_id": user_id}).scalars()
    post = Post.query.filter(Post.id.in_(existing_subs), Post.deleted == False,
                             Post.status > POST_STATUS_REVIEWING).first()
    assert post is not None and hasattr(post, 'id')
    if post:
        data = {"post_id": post.id, "subscribe": True}
        with pytest.raises(Exception) as ex:
            put_post_subscribe(auth, data)
        assert str(ex.value) == 'A subscription for this post already existed.'

    # add to deleted
    post = Post.query.filter(Post.deleted == True).first()
    if post:
        data = {"post_id": post.id, "subscribe": True}
        with pytest.raises(Exception):
            result = put_post_subscribe(auth, data)

    # remove from deleted
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 3'),
        {"user_id": user_id}).scalars()
    post = Post.query.filter(Post.id.in_(existing_subs), Post.deleted == True).first()
    if post:
        data = {"post_id": post.id, "subscribe": False}
        with pytest.raises(Exception):
            result = put_post_subscribe(auth, data)
