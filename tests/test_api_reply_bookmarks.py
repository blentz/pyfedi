import pytest
from flask import g
from sqlalchemy import text

from app import db
from app.models import Post, PostReply, User


def test_api_reply_bookmarks(app, api_baseline):
    from app.api.alpha.utils.reply import put_reply_save

    # reply_view (called by put_reply_save) unconditionally reads g.admin_ids.
    # See test_api_post_bookmarks.py for why this must be set explicitly here.
    g.admin_ids = []

    user_id = api_baseline.user1.id
    user = db.session.get(User, user_id)
    assert user is not None and hasattr(user, 'id')
    jwt = user.encode_jwt_token()
    assert jwt is not None
    auth = f'Bearer {jwt}'

    # normal add / remove bookmark
    existing_bookmarks = db.session.execute(
        text('SELECT post_reply_id FROM "post_reply_bookmark" WHERE user_id = :user_id'),
        {"user_id": user_id}).scalars()
    reply = PostReply.query.filter(PostReply.id.not_in(existing_bookmarks), PostReply.deleted == False).first()
    assert reply is not None and hasattr(reply, 'id')

    data = {"comment_id": reply.id, "save": True}
    result = put_reply_save(auth, data)
    assert result is not None and result['comment_view']['saved'] == True
    data = {"comment_id": reply.id, "save": False}
    result = put_reply_save(auth, data)
    assert result is not None and result['comment_view']['saved'] == False

    # remove from non-existing
    data = {"comment_id": reply.id, "save": False}
    with pytest.raises(Exception) as ex:
        put_reply_save(auth, data)
    assert str(ex.value) == 'This comment was not bookmarked.'

    # add to existing
    existing_bookmarks = db.session.execute(
        text('SELECT post_reply_id FROM "post_reply_bookmark" WHERE user_id = :user_id'),
        {"user_id": user_id}).scalars()
    reply = PostReply.query.filter(PostReply.id.in_(existing_bookmarks), PostReply.deleted == False).first()
    if reply:
        data = {"comment_id": reply.id, "save": True}
        with pytest.raises(Exception) as ex:
            put_reply_save(auth, data)
        assert str(ex.value) == 'This comment has already been bookmarked.'

    # add to deleted (reply or post)
    reply = PostReply.query.filter(PostReply.deleted == True).first()
    if reply:
        data = {"comment_id": reply.id, "save": True}
        with pytest.raises(Exception):
            result = put_reply_save(auth, data)
    reply = PostReply.query.filter_by(deleted=True).join(Post, Post.id == PostReply.post_id).filter_by(
        deleted=True).first()
    if reply:
        data = {"comment_id": reply.id, "save": True}
        with pytest.raises(Exception):
            result = put_reply_save(auth, data)

    # remove from deleted (reply or post)
    existing_bookmarks = db.session.execute(
        text('SELECT post_reply_id FROM "post_reply_bookmark" WHERE user_id = :user_id'),
        {"user_id": user_id}).scalars()
    reply = PostReply.query.filter(PostReply.id.in_(existing_bookmarks), PostReply.deleted == True).first()
    if reply:
        data = {"comment_id": reply.id, "save": False}
        with pytest.raises(Exception):
            result = put_reply_save(auth, data)
    existing_bookmarks = db.session.execute(
        text('SELECT post_reply_id FROM "post_reply_bookmark" WHERE user_id = :user_id'),
        {"user_id": user_id}).scalars()
    reply = PostReply.query.filter(PostReply.id.in_(existing_bookmarks), PostReply.deleted == True).join(Post,
                                                                                                         Post.id == PostReply.post_id).filter_by(
        deleted=True).first()
    if reply:
        data = {"comment_id": reply.id, "save": False}
        with pytest.raises(Exception):
            result = put_reply_save(auth, data)
